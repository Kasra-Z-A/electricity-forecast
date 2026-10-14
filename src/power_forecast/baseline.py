"""Cutoff-aware local-clock seasonal baselines and immutable forecast artifacts."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from power_forecast.history import atomic_json, historical_versions, resolution
from power_forecast.ingestion.entsoe import SourceError
from power_forecast.timegrid import VIENNA, delivery_grid, forecast_cutoff

# Each model tries its first lag; missing slots use the most recent eligible match.
WEEKLY_LAGS = [7] + [n for n in range(1, 29) if n != 7]
DAILY_LAGS = list(range(1, 29))
MODELS = {"seasonal-weekly-v1": WEEKLY_LAGS, "seasonal-daily-v1": DAILY_LAGS}
# Daily replaced weekly on 2026-10-14 after the validation backtest
# (reports/backtest_baselines.json); weekly stays the skill reference.
PRODUCTION_MODEL = "seasonal-daily-v1"


def slot_lookup(snapshots) -> dict:
    """Index (record, rows) pairs by local date, hour and minute."""
    lookup = {}
    for record, rows in snapshots:
        for row in rows:
            local = row.start.astimezone(VIENNA)
            lookup.setdefault((local.date(), local.hour, local.minute), []).append((row, record))
    return lookup


def matching_slot(lookup: dict, day: date, target: datetime, lag: int):
    """The (row, record) for `target`'s local clock slot `lag` days earlier, if archived.

    Hourly source days match on the hour. On DST days the matching fold wins;
    otherwise the only matching clock slot serves both folds.
    """
    local = target.astimezone(VIENNA)
    source_day = day - timedelta(days=lag)
    minute = 0 if resolution(source_day) == 60 else local.minute
    candidates = lookup.get((source_day, local.hour, minute), [])
    if not candidates:
        return None
    return next(
        (item for item in candidates if item[0].start.astimezone(VIENNA).fold == local.fold),
        candidates[0],
    )


def forecast(
    day: date,
    data_dir: Path,
    replay: bool = False,
    now: datetime | None = None,
    model: str = PRODUCTION_MODEL,
) -> Path:
    lags = MODELS[model]
    now = now or datetime.now(UTC)
    cutoff = forecast_cutoff(day)
    if not replay and now < cutoff:
        raise SourceError("Official cutoff has not arrived; use --replay for an exploratory run.")
    lookup = slot_lookup(historical_versions(data_dir, day, cutoff, replay))
    predictions = []
    used = {}
    for target in delivery_grid(day, resolution(day)):
        match = None
        for lag in lags:
            match = matching_slot(lookup, day, target, lag)
            if match:
                break
        if match is None:
            raise SourceError(
                f"No eligible matching history for {target.isoformat()}; forecast unavailable."
            )
        row, record = match
        used[record["sha256"]] = record
        predictions.append(
            {
                "start": target.isoformat(),
                "minutes": resolution(day),
                "point": row.eur_mwh,
                "source_start": row.start.isoformat(),
                "source_snapshot": record["sha256"],
                "lag_days": lag,
                "fallback": lag != lags[0] or row.minutes != resolution(day),
                "hourly_derived": row.minutes != resolution(day),
            }
        )
    code = hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob("*.py")):
        code.update(str(path.relative_to(Path(__file__).parent)).encode())
        code.update(path.read_bytes())
    lock = Path("uv.lock")
    artifact = {
        "schema_version": 1,
        "model": model,
        "delivery_date": str(day),
        "cutoff_at": cutoff.isoformat(),
        "issued_at": now.isoformat(),
        "mode": "reconstructed_replay" if replay else "live",
        "status": "replay"
        if replay
        else ("on_time" if now <= cutoff + timedelta(minutes=15) else "late"),
        "timezone": "Europe/Vienna",
        "unit": "EUR/MWh",
        "code_sha256": code.hexdigest(),
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
        "input_snapshots": list(used.values()),
        "predictions": predictions,
    }
    run_id = hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest()
    output = data_dir / "forecasts" / f"{run_id}.json"
    atomic_json(output, artifact | {"run_id": run_id})
    return output
