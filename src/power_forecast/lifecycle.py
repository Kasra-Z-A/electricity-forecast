"""Learned candidates in shadow: weekly refits and a forecast beside the official one.

A candidate is refitted at most every REFIT_DAYS, at a forecast cutoff, on the
labels available then, like the rolling-origin backtests. Its forecast for D uses
only price versions retrieved before D's cutoff. Shadow artifacts live under
`shadow/`, apart from `forecasts/`, so they never reach the public dashboard.

A vintage replay recreates past days from the versions the host retrieved before
each cutoff. Its training labels may have been retrieved later (the host backfilled
older days afterwards); such fits are marked `reconstructed_labels`.
"""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from power_forecast import ridge
from power_forecast.baseline import code_sha256, slot_lookup
from power_forecast.baseline import forecast as baseline_forecast
from power_forecast.features import FEATURES, day_features
from power_forecast.history import (
    TRANSITION,
    atomic_json,
    historical_versions,
    load_version,
    resolution,
)
from power_forecast.ingestion.entsoe import SourceError
from power_forecast.timegrid import delivery_grid, forecast_cutoff, results_expected

# Mirrors configs/candidate.json, which is not part of the image; a test keeps them equal.
CANDIDATE = {
    "name": "ridge-core-180-v1",
    "kind": "ridge",
    "params": {"alpha": 1.0},
    "features": "core",
    "training_window": 180,
    "seed": 0,
}
CANDIDATE_INFO = {
    "name": "Ridge regression",
    "description": "A linear regression on recent prices at the same time of day and on "
    "calendar features, retrained every week on the last 180 days.",
}
REFIT_DAYS = 7
# Comparison models (challengers.py): forecast daily, never published or promoted.
# Settings of the M8 comparison (configs/comparison.json, configs/backtest_models.json).
CHALLENGERS = {
    "lightgbm-wl-180-v1": {
        "kind": "lightgbm",
        "params": {
            "objective": "l1",
            "n_estimators": 400,
            "learning_rate": 0.05,
            "num_leaves": 31,
            "min_child_samples": 50,
            "subsample": 0.8,
            "subsample_freq": 1,
            "colsample_bytree": 0.8,
        },
        "covariates": ["weather", "load"],
        "training_window": 180,
        "seed": 0,
    },
    "lear-wl-v1": {
        "kind": "lear",
        "covariates": ["weather", "load"],
        "training_windows": [56, 84, 182],
    },
    "chronos2-zs-v1": {
        "kind": "chronos",
        "covariates": ["weather", "load"],
        "context_days": 84,
        "quantiles": [0.1, 0.5, 0.9],
    },
}
# Retired models stop forecasting and leave the gate and the dashboard; their past
# forecasts stay in the archive. Retirement follows the rule in docs/DESIGN.md.
RETIRED: frozenset = frozenset()
# A learned champion that fails at publication falls back along this chain before
# the seasonal baseline.
FALLBACKS = {"chronos2-zs-v1": ["lear-wl-v1"]}
CHALLENGER_INFO = {
    "lightgbm-wl-180-v1": {
        "name": "LightGBM",
        "description": "Gradient-boosted trees on recent prices, calendar features, the "
        "weather forecast and the load forecast, retrained every week on the last 180 days.",
    },
    "lear-wl-v1": {
        "name": "LEAR",
        "description": "One regularised linear regression per quarter-hour on the previous "
        "days' prices, the load and weather forecasts, averaged over three training windows "
        "and retrained every week.",
    },
    "chronos2-zs-v1": {
        "name": "Chronos-2",
        "description": "A pretrained time-series foundation model, used without retraining: "
        "it reads the last 84 days of prices with their load and weather forecasts, and the "
        "forecasts for the delivery day.",
    },
}


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def prices_at(data_dir: Path, moment: datetime | None) -> dict:
    """Latest version per day retrieved by `moment` (any time if None): {day: (value, rows)}."""
    selected = {}
    for path in (data_dir / "prices" / "versions").glob("*.json"):
        value = json.loads(path.read_text())
        retrieved = datetime.fromisoformat(value["retrieved_at"])
        if moment is not None and retrieved > moment:
            continue
        day = date.fromisoformat(value["delivery_date"])
        if day not in selected or retrieved > selected[day][0]:
            selected[day] = (retrieved, path)
    archive = {}
    for day, (_, path) in selected.items():
        record = {"path": str(path.relative_to(data_dir)), "sha256": path.stem}
        archive[day] = load_version(data_dir, record)
    return archive


def last_label(moment: datetime) -> date:
    """Newest delivery day whose auction result is expected by `moment`."""
    day = moment.astimezone(UTC).date() + timedelta(days=1)
    while results_expected(day) > moment:
        day -= timedelta(days=1)
    return day


def fit(data_dir: Path, moment: datetime, reconstructed_labels: bool = False) -> dict:
    """Fit the candidate on the labels available at `moment` and store it."""
    archive = prices_at(data_dir, None if reconstructed_labels else moment)
    lookup = slot_lookup(archive.values())
    train_last = last_label(moment)
    train_first = max(TRANSITION, train_last - timedelta(days=CANDIDATE["training_window"] - 1))
    features, targets, used = [], [], {}
    day = train_first
    while day <= train_last:
        rows = day_features(archive, lookup, day) if day in archive else None
        if rows is not None:
            features += rows
            targets += [row.eur_mwh for row in archive[day][1]]
            used[str(day)] = _digest(archive[day][0])
        day += timedelta(days=1)
    if len(used) < 28:
        raise SourceError(f"Only {len(used)} trainable days before {moment}; backfill prices.")
    body = {
        "schema_version": 1,
        "candidate": CANDIDATE,
        "features": list(FEATURES),
        "trained_at": moment.isoformat(),
        "train_first": str(train_first),
        "train_last": str(train_last),
        "training_days": len(used),
        "training_rows": len(targets),
        "label_versions_sha256": _digest(used),
        "reconstructed_labels": reconstructed_labels,
        "code_sha256": code_sha256(),
        "params": ridge.fit(features, targets, CANDIDATE["params"]["alpha"]),
    }
    fit_id = _digest(body)
    atomic_json(data_dir / "models" / "fits" / f"{fit_id}.json", body | {"fit_id": fit_id})
    return body | {"fit_id": fit_id}


def current_fit(data_dir: Path, moment: datetime, reconstructed_labels: bool = False) -> dict:
    """The newest fit made by `moment` within REFIT_DAYS, otherwise a new one."""
    newest = None
    for path in (data_dir / "models" / "fits").glob("*.json"):
        value = json.loads(path.read_text())
        if value["fit_id"] != _digest({k: v for k, v in value.items() if k != "fit_id"}):
            raise SourceError("Model fit checksum mismatch.")
        trained = datetime.fromisoformat(value["trained_at"])
        if value["candidate"] != CANDIDATE or value["reconstructed_labels"] != reconstructed_labels:
            continue
        if trained <= moment and (newest is None or trained > newest[0]):
            newest = (trained, value)
    if newest and moment - newest[0] < timedelta(days=REFIT_DAYS):
        return newest[1]
    return fit(data_dir, moment, reconstructed_labels)


def pointer_path(data_dir: Path, model: str, day: date) -> Path:
    return data_dir / "shadow" / "days" / model / f"{day}.json"


def candidate_forecast(
    day: date, data_dir: Path, now: datetime, role: str, vintage: bool = False
) -> Path:
    """Write the candidate's forecast for `day`: official runs go to `forecasts/`,
    shadow runs and replays to `shadow/forecasts/`."""
    cutoff = forecast_cutoff(day)
    if not vintage and now < cutoff:
        raise SourceError("Learned forecasts run after the cutoff.")
    # A live refit happens at the cutoff, so it sees no version retrieved after it.
    model = current_fit(data_dir, cutoff, reconstructed_labels=vintage)
    snapshots = historical_versions(data_dir, day, cutoff, replay=False)
    archive = {
        date.fromisoformat(record["delivery_date"]): (record, rows) for record, rows in snapshots
    }
    rows = day_features(archive, slot_lookup(snapshots), day)
    if rows is None:
        raise SourceError(f"No eligible feature history for {day}.")
    points = ridge.predict(model["params"], rows)
    grid = delivery_grid(day, resolution(day))
    lock = Path("uv.lock")
    artifact = {
        "schema_version": 1,
        "model": CANDIDATE["name"],
        "fit_id": model["fit_id"],
        "role": role,
        "delivery_date": str(day),
        "cutoff_at": cutoff.isoformat(),
        "issued_at": now.isoformat(),
        "mode": "vintage_replay" if vintage else "live",
        "status": "replay"
        if vintage
        else ("on_time" if now <= cutoff + timedelta(minutes=15) else "late"),
        "training_labels": "reconstructed" if model["reconstructed_labels"] else "vintage",
        "timezone": "Europe/Vienna",
        "unit": "EUR/MWh",
        "code_sha256": code_sha256(),
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
        "input_snapshots": [record for record, _ in snapshots],
        "predictions": [
            {
                "start": start.isoformat(),
                "minutes": resolution(day),
                "point": point,
                "fallback": False,
            }
            for start, point in zip(grid, points, strict=True)
        ],
    }
    run_id = _digest(artifact)
    directory = "forecasts" if role == "official" else "shadow/forecasts"
    output = data_dir / directory / f"{run_id}.json"
    atomic_json(output, artifact | {"run_id": run_id})
    return output


def _point_to(data_dir: Path, model: str, day: date, output: Path, mode: str) -> dict:
    record = {
        "model": model,
        "delivery_date": str(day),
        "forecast_path": str(output.relative_to(data_dir)),
        "mode": mode,
    }
    atomic_json(pointer_path(data_dir, model, day), record)
    return record


def shadow_forecast(day: date, data_dir: Path, now: datetime, vintage: bool = False) -> dict:
    """Forecast `day` with the candidate once; an existing pointer is returned unchanged."""
    pointer = pointer_path(data_dir, CANDIDATE["name"], day)
    if pointer.exists():
        return json.loads(pointer.read_text()) | {"reused": True}
    output = candidate_forecast(day, data_dir, now, "shadow", vintage)
    return _point_to(
        data_dir, CANDIDATE["name"], day, output, "vintage_replay" if vintage else "live"
    )


def reference_forecast(day: date, data_dir: Path, model: str) -> dict:
    """A seasonal baseline for a past day from the versions retrieved before its cutoff.

    It is what the live job would have published, so the gate can compare a candidate
    with a champion or the weekly reference on days that champion was not official.
    """
    pointer = pointer_path(data_dir, model, day)
    if pointer.exists():
        return json.loads(pointer.read_text())
    output = baseline_forecast(
        day,
        data_dir,
        now=forecast_cutoff(day),
        model=model,
        vintage=True,
        directory="shadow/forecasts",
    )
    return _point_to(data_dir, model, day, output, "vintage_replay")
