"""Cutoff-aware local-clock weekly baseline and immutable forecast artifacts."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from power_forecast.history import atomic_json, historical_versions, resolution
from power_forecast.ingestion.entsoe import SourceError
from power_forecast.timegrid import VIENNA, delivery_grid, forecast_cutoff


def forecast(day: date, data_dir: Path, replay: bool = False, now: datetime | None = None) -> Path:
    now = now or datetime.now(UTC)
    cutoff = forecast_cutoff(day)
    if not replay and now < cutoff:
        raise SourceError("Official cutoff has not arrived; use --replay for an exploratory run.")
    snapshots = historical_versions(data_dir, day, cutoff, replay)
    lookup = {}
    for record, rows in snapshots:
        for row in rows:
            local = row.start.astimezone(VIENNA)
            lookup.setdefault((local.date(), local.hour, local.minute), []).append((row, record))
    predictions = []
    used = {}
    for target in delivery_grid(day, resolution(day)):
        local = target.astimezone(VIENNA)
        # Weekly first; missing slots use the most recent eligible matching slot.
        offsets = [7] + [n for n in range(1, 29) if n != 7]
        match = None
        for lag in offsets:
            source_day = day - timedelta(days=lag)
            minute = 0 if resolution(source_day) == 60 else local.minute
            candidates = lookup.get((source_day, local.hour, minute), [])
            if candidates:
                match = next(
                    (
                        item
                        for item in candidates
                        if item[0].start.astimezone(VIENNA).fold == local.fold
                    ),
                    candidates[0],
                )
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
                "fallback": lag != 7 or row.minutes != resolution(day),
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
        "model": "seasonal-weekly-v1",
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
