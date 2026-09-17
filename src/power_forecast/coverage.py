"""Audit what the price archive actually contains, before splits are frozen.

Expected interval counts are derived from the same grid the ingestion path
validates against, so a DST day or the quarter-hour transition is described by
the contract rather than by numbers repeated here.
"""

import json
from datetime import date, timedelta
from pathlib import Path

from power_forecast.history import TRANSITION, load_version, resolution
from power_forecast.ingestion.entsoe import SourceError
from power_forecast.timegrid import delivery_grid


def expected_intervals(day: date) -> int:
    return len(delivery_grid(day, resolution(day)))


def checkpoints(data_dir: Path) -> dict[date, dict]:
    records = {}
    for path in sorted((data_dir / "prices" / "days").glob("*.json")):
        record = json.loads(path.read_text())
        records[date.fromisoformat(record["delivery_date"])] = record
    return records


def _gaps(missing: list[date]) -> list[dict]:
    runs = []
    for day in missing:
        if runs and day - date.fromisoformat(runs[-1]["last"]) == timedelta(days=1):
            runs[-1]["last"] = str(day)
            runs[-1]["days"] += 1
        else:
            runs.append({"first": str(day), "last": str(day), "days": 1})
    return runs


def _era(days: list[date], minutes: int) -> dict:
    return {
        "days": len(days),
        "resolution_minutes": minutes,
        "first": str(days[0]) if days else None,
        "last": str(days[-1]) if days else None,
    }


def coverage(data_dir: Path, verify: bool = False) -> dict:
    """Describe archive completeness. `verify` re-checks every stored checksum."""
    records = checkpoints(data_dir)
    days = sorted(records)
    if not days:
        return {"days": 0, "span": None, "complete": False}

    first, last = days[0], days[-1]
    present = set(days)
    missing = [
        first + timedelta(days=offset)
        for offset in range((last - first).days + 1)
        if first + timedelta(days=offset) not in present
    ]

    counts: dict[int, int] = {}
    mismatches = []
    for day in days:
        actual = records[day]["intervals"]
        counts[actual] = counts.get(actual, 0) + 1
        expected = expected_intervals(day)
        if actual != expected:
            mismatches.append({"day": str(day), "expected": expected, "actual": actual})

    failures = []
    if verify:
        for day in days:
            try:
                load_version(data_dir, records[day])
            except SourceError as error:
                failures.append({"day": str(day), "error": str(error)})

    return {
        "days": len(days),
        "span": {"first": str(first), "last": str(last)},
        "missing_days": len(missing),
        "gaps": _gaps(missing),
        "eras": {
            "hourly": _era([d for d in days if d < TRANSITION], 60),
            "native": _era([d for d in days if d >= TRANSITION], 15),
        },
        "interval_counts": {str(k): counts[k] for k in sorted(counts)},
        "interval_mismatches": mismatches,
        "checksums_verified": verify,
        "checksum_failures": failures,
        "complete": not missing and not mismatches and not failures,
        "caveat": (
            "Describes archived coverage only. It does not establish what was "
            "retrievable at any past cutoff, and retrospective days may carry "
            "later source revisions."
        ),
    }
