"""Challengers: candidates beyond ridge that forecast every day in shadow.

Each challenger refits once a week on a fixed anchor: the fit used for delivery day
D is made at the cutoff of the latest Monday on or before D, on the price labels
retrieved by then. Fits are rebuilt from the archive on each run rather than
stored, so a run needs only the data. Training days take their weather and load
from the archive, which for older days was retrieved after their cutoffs, as in the
M8 comparison. The forecast for D uses price versions retrieved before D's cutoff
and the weather run and load forecast archived for D. A live forecast requires both
covariates to have been retrieved before the cutoff. A vintage replay of a past day
accepts later retrievals and trains on every label now archived (older labels were
backfilled after the fact); it records both as reconstructed.

Shadow artifacts go to `shadow/forecasts/` with a pointer under
`shadow/days/<model>/`, so they never reach `forecasts/` or the official pointer.
The promotion gate evaluates them like the ridge candidate; once one is champion,
publish writes its forecast as the official one and this job skips it. Needs the
`research` dependency group.
"""

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from power_forecast import lear
from power_forecast.baseline import code_sha256
from power_forecast.history import TRANSITION, atomic_json, resolution
from power_forecast.ingestion.entsoe import SourceError
from power_forecast.ingestion.load import load_load
from power_forecast.ingestion.weather import load_weather
from power_forecast.lifecycle import (
    CHALLENGERS,
    _digest,
    _point_to,
    last_label,
    pointer_path,
    prices_at,
)
from power_forecast.models import FeatureCache, build
from power_forecast.timegrid import delivery_grid, forecast_cutoff

# A Monday; every anchor is a whole number of weeks after it.
ANCHOR = date(2026, 1, 5)
REPLAY_DAYS = 28


def anchor(day: date) -> datetime:
    """The cutoff at which the fit used for `day` is made."""
    return forecast_cutoff(day - timedelta(days=(day - ANCHOR).days % 7))


class Inputs:
    """Archives read once per run, with fits cached by model and anchor."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.covariates = {"weather": load_weather(data_dir), "load": load_load(data_dir)}
        self.prices = {}
        self.fits = {}
        # Chronos-2 runs in a separate process for a batch of days (chronos.py).
        self.chronos = {}

    def run_chronos(self, days: list[date]) -> None:
        from power_forecast import chronos

        self.chronos |= chronos.forecast_isolated(self.data_dir, days, point_in_time=True)

    def prices_at(self, moment: datetime | None) -> dict:
        if moment not in self.prices:
            self.prices[moment] = prices_at(self.data_dir, moment)
        return self.prices[moment]

    def fit(self, model: str, moment: datetime, vintage: bool):
        key = (model, moment, vintage)
        if key not in self.fits:
            labels = self.prices_at(None if vintage else moment)
            self.fits[key] = _fit(model, labels, self.covariates, moment)
        return self.fits[key]


def _fit(model: str, archive: dict, covariates: dict, moment: datetime) -> dict:
    spec = CHALLENGERS[model]
    last = last_label(moment)
    if spec["kind"] == "chronos":
        return {}  # zero-shot: nothing is fitted
    if spec["kind"] == "lightgbm":
        first = max(TRANSITION, last - timedelta(days=spec["training_window"] - 1))
        cache = FeatureCache(archive, covariates)
        features, targets = cache.matrix(first, last)
        if len(targets) < 28 * 96:
            raise SourceError(f"Only {len(targets)} training rows before {moment}.")
        estimator = build("lightgbm", spec["params"], spec["seed"]).fit(features, targets)
        return {"estimator": estimator, "train_first": first, "train_last": last}
    inputs = lear.LearInputs(archive, covariates)
    windows = []
    for window in spec["training_windows"]:
        first = max(TRANSITION, last - timedelta(days=window - 1))
        fitted = lear.fit_window(inputs, first, last)
        if fitted is not None:
            windows.append(fitted)
    if not windows:
        raise SourceError(f"No LEAR window has enough training days before {moment}.")
    return {"windows": windows, "train_first": first, "train_last": last}


def _predict(model: str, fitted: dict, inputs: Inputs, archive: dict, covariates, day) -> list:
    """Point forecasts on `day`'s grid, with (P10, P90) for Chronos-2."""
    grid = delivery_grid(day, resolution(day))
    kind = CHALLENGERS[model]["kind"]
    if kind == "chronos":
        if day not in inputs.chronos:
            raise SourceError(f"No Chronos-2 forecast for {day}.")
        return [(float(q[1]), (float(q[0]), float(q[2]))) for q in np.sort(inputs.chronos[day])]
    if kind == "lightgbm":
        rows = FeatureCache(archive, covariates)(day)
        if rows is None:
            raise SourceError(f"No eligible feature history for {day}.")
        points = fitted["estimator"].predict(np.array(rows, dtype=float))
        return [(float(p), None) for p in points]
    row = lear.LearInputs(archive, covariates)(day)
    if row is None:
        raise SourceError(f"No eligible LEAR inputs for {day}.")
    curve = np.mean([lear.predict_window(f, row) for f in fitted["windows"]], axis=0)
    return [(float(curve[lear.slot(start)]), None) for start in grid]


def eligible(model: str, day: date, inputs: Inputs, now: datetime, vintage: bool) -> bool:
    """Whether `day` may be forecast now: raises SourceError with the reason if not."""
    cutoff = forecast_cutoff(day)
    if not vintage and now < cutoff:
        raise SourceError("Learned forecasts run after the cutoff.")
    reconstructed = False
    for name in CHALLENGERS[model]["covariates"]:
        value = inputs.covariates[name].get(day)
        if value is None:
            raise SourceError(f"No archived {name} forecast for {day}.")
        late = datetime.fromisoformat(value["retrieved_at"]) > cutoff
        if late and not vintage:
            raise SourceError(f"The {name} forecast for {day} was retrieved after the cutoff.")
        reconstructed |= late
    return reconstructed


def _fit_record(model: str, fitted: dict, day: date) -> dict:
    spec = CHALLENGERS[model]
    if spec["kind"] == "chronos":
        from power_forecast import chronos

        return {
            "weights": chronos.MODEL,
            "revision": chronos.REVISION,
            "weights_sha256": chronos.WEIGHTS_SHA256,
        }
    return {
        "made_at": anchor(day).isoformat(),
        "train_first": str(fitted["train_first"]),
        "train_last": str(fitted["train_last"]),
    }


def forecast(
    model: str, day: date, inputs: Inputs, now: datetime, vintage: bool, role: str = "shadow"
) -> Path:
    """Write the challenger's forecast for `day` and return its path; official runs go to
    `forecasts/`, shadow runs and replays to `shadow/forecasts/`."""
    cutoff = forecast_cutoff(day)
    reconstructed = eligible(model, day, inputs, now, vintage)
    covariates = {name: inputs.covariates[name] for name in CHALLENGERS[model]["covariates"]}
    fitted = inputs.fit(model, anchor(day), vintage)
    archive = inputs.prices_at(cutoff)
    points = _predict(model, fitted, inputs, archive, covariates, day)
    zero_shot = CHALLENGERS[model]["kind"] == "chronos"
    lock = Path("uv.lock")
    artifact = {
        "schema_version": 1,
        "model": model,
        "spec": CHALLENGERS[model],
        "role": role,
        "delivery_date": str(day),
        "cutoff_at": cutoff.isoformat(),
        "issued_at": now.isoformat(),
        "mode": "vintage_replay" if vintage else "live",
        "status": "replay"
        if vintage
        else ("on_time" if now <= cutoff + timedelta(minutes=15) else "late"),
        "fit": _fit_record(model, fitted, day),
        "training_labels": "none" if zero_shot else ("reconstructed" if vintage else "vintage"),
        "covariates": "reconstructed" if reconstructed else "retrieved_before_cutoff",
        "timezone": "Europe/Vienna",
        "unit": "EUR/MWh",
        "code_sha256": code_sha256(),
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
        "input_snapshots": {
            "prices": sorted(
                _digest(archive[d][0]) for d in archive if day - timedelta(days=15) <= d < day
            ),
            **{
                name: hashlib.sha256(
                    json.dumps(inputs.covariates[name][day], sort_keys=True).encode()
                ).hexdigest()
                for name in covariates
            },
        },
        "predictions": [
            {
                "start": start.isoformat(),
                "minutes": resolution(day),
                "point": point,
                "fallback": False,
            }
            | ({"p10": band[0], "p90": band[1]} if band else {})
            for start, (point, band) in zip(
                delivery_grid(day, resolution(day)), points, strict=True
            )
        ],
    }
    run_id = _digest(artifact)
    directory = "forecasts" if role == "official" else "shadow/forecasts"
    output = inputs.data_dir / directory / f"{run_id}.json"
    atomic_json(output, artifact | {"run_id": run_id})
    return output


def official_forecast(model: str, day: date, data_dir: Path, now: datetime) -> Path:
    """The champion challenger's forecast for publication."""
    inputs = Inputs(data_dir)
    if CHALLENGERS[model]["kind"] == "chronos":
        eligible(model, day, inputs, now, vintage=False)
        inputs.run_chronos([day])
    return forecast(model, day, inputs, now, vintage=False, role="official")


def _batch_chronos(model: str, day: date, inputs: Inputs, now: datetime, outcome: dict) -> None:
    """Run Chronos-2 once for every pending day; a failed run marks all of them."""
    pending = []
    for offset in range(REPLAY_DAYS, -1, -1):
        target = day - timedelta(days=offset)
        if pointer_path(inputs.data_dir, model, target).exists():
            continue
        try:
            eligible(model, target, inputs, now, vintage=offset > 0)
        except SourceError:
            continue  # reported by the per-day loop
        pending.append(target)
    if not pending:
        return
    try:
        inputs.run_chronos(pending)
    except Exception as error:  # noqa: BLE001 - one challenger must not stop another
        for target in pending:
            outcome[str(target)] = {"status": "error", "error_type": type(error).__name__}


def run_challengers(day: date, data_dir: Path, now: datetime, champion: str) -> dict:
    """Forecast `day` live with every challenger, and replay missing recent days.

    The champion is skipped: publish makes its forecast. A day that already has a
    pointer is kept. Failures are reported per model and day and never raise, so one
    challenger cannot stop another.
    """
    inputs = Inputs(data_dir)
    report = {}
    for model in [m for m in CHALLENGERS if m != champion]:
        outcome = {}
        if CHALLENGERS[model]["kind"] == "chronos":
            _batch_chronos(model, day, inputs, now, outcome)
        for offset in range(REPLAY_DAYS, -1, -1):
            target = day - timedelta(days=offset)
            if pointer_path(data_dir, model, target).exists() or str(target) in outcome:
                continue
            vintage = offset > 0
            try:
                output = forecast(model, target, inputs, now, vintage)
            except (SourceError, ValueError) as error:
                outcome[str(target)] = {"status": "error", "error_type": type(error).__name__}
                # Messages of this module's own errors name only dates and inputs.
                if isinstance(error, SourceError):
                    outcome[str(target)]["error"] = str(error)
                continue
            _point_to(data_dir, model, target, output, "vintage_replay" if vintage else "live")
            outcome[str(target)] = {"status": "vintage_replay" if vintage else "live"}
        report[model] = outcome
    live = [outcome.get(str(day), {"status": "kept"})["status"] for outcome in report.values()]
    failed = any(status == "error" for status in live)
    return {"status": "partial" if failed else "success", "models": report}
