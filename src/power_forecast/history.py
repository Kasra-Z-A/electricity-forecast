"""Resumable daily price partitions with immutable versions and atomic checkpoints."""

import hashlib
import json
import math
import os
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from power_forecast.ingestion.entsoe import (
    AREA,
    Price,
    SourceError,
    archive,
    fetch_prices,
    parse_prices,
)
from power_forecast.timegrid import day_bounds, delivery_grid

TRANSITION = date(2025, 10, 1)


def resolution(day: date) -> int:
    return 15 if day >= TRANSITION else 60


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_day(day: date, rows: list[Price]) -> None:
    if [r.start for r in rows] != delivery_grid(day, resolution(day)) or any(
        r.minutes != resolution(day) or not math.isfinite(r.eur_mwh) for r in rows
    ):
        raise SourceError(f"Incomplete or invalid price grid for {day}.")


def ingest_day(
    day: date, key: str, data_dir: Path, refresh: bool = False, area: str = AREA
) -> dict:
    """Ingest one local delivery day. Each bidding zone needs its own `data_dir`."""
    checkpoint = data_dir / "prices" / "days" / f"{day}.json"
    if checkpoint.exists() and not refresh:
        record = json.loads(checkpoint.read_text())
        load_version(data_dir, record)
        return record | {"cached": True}
    start, end = day_bounds(day)
    payload = fetch_prices(key, start, end, area)
    raw = archive(payload, data_dir / "raw" / "entsoe", start, end, area)
    retrieved_at = datetime.now(UTC).isoformat()
    rows = [r for r in parse_prices(payload, area) if start <= r.start < end]
    validate_day(day, rows)
    normalized = {
        "schema_version": 1,
        "delivery_date": str(day),
        "raw_sha256": raw.stem,
        "retrieved_at": retrieved_at,
        "resolution_minutes": resolution(day),
        "prices": [
            {
                "start": r.start.isoformat(),
                "minutes": r.minutes,
                "eur_mwh": r.eur_mwh,
                "document_id": r.document_id,
                "revision": r.revision,
            }
            for r in rows
        ],
    }
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    relative = Path("prices") / "versions" / f"{digest}.json"
    atomic_json(data_dir / relative, normalized)
    record = {
        "path": str(relative),
        "sha256": digest,
        "retrieved_at": retrieved_at,
        "delivery_date": str(day),
        "intervals": len(rows),
    }
    # The checkpoint is advanced only after the complete immutable partition exists.
    atomic_json(checkpoint, record)
    return record | {"cached": False}


def load_version(data_dir: Path, record: dict) -> tuple[dict, list[Price]]:
    value = json.loads((data_dir / record["path"]).read_text())
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    if digest != record["sha256"]:
        raise SourceError("Normalized price snapshot checksum mismatch.")
    rows = [
        Price(
            datetime.fromisoformat(r["start"]),
            r["minutes"],
            r["eur_mwh"],
            r["document_id"],
            r["revision"],
        )
        for r in value["prices"]
    ]
    validate_day(date.fromisoformat(value["delivery_date"]), rows)
    return value, rows


def historical_versions(
    data_dir: Path, day: date, cutoff: datetime, replay: bool
) -> list[tuple[dict, list[Price]]]:
    """Select latest eligible version per past day, never latest-file order."""
    selected = {}
    for path in sorted((data_dir / "prices" / "versions").glob("*.json")):
        value = json.loads(path.read_text())
        source_day = date.fromisoformat(value["delivery_date"])
        retrieved = datetime.fromisoformat(value["retrieved_at"])
        if not day - timedelta(days=28) <= source_day < day:
            continue
        if not replay and retrieved > cutoff:
            continue
        record = {
            "path": str(path.relative_to(data_dir)),
            "sha256": path.stem,
            "retrieved_at": value["retrieved_at"],
            "delivery_date": str(source_day),
        }
        if source_day not in selected or retrieved > selected[source_day][0]:
            selected[source_day] = (retrieved, record)
    return [
        (record, load_version(data_dir, record)[1])
        for _, record in sorted(selected.values(), key=lambda item: item[1]["delivery_date"])
    ]
