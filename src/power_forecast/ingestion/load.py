"""ENTSO-E day-ahead total load forecast for AT, one document per delivery day.

Regulation 543/2013 Art. 6(2)(b) makes it due two hours before day-ahead gate
closure, 10:00 on D-1, before the 10:30 cutoff. ENTSO-E records no publication
time, so historical days are reconstructed, not verified vintages; the prospective
10:00 log in observations.py is the availability check (docs/SOURCE_AUDIT.md).
"""

import hashlib
import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from defusedxml import ElementTree as ET

from power_forecast.history import atomic_json
from power_forecast.ingestion.entsoe import AREA, SourceError, fetch_document
from power_forecast.timegrid import day_bounds, delivery_grid

QUERY = {"documentType": "A65", "processType": "A01", "outBiddingZone_Domain": AREA}


def parse_load(payload: bytes, day: date) -> list[float]:
    """Forecast MW per quarter-hour of `day` in UTC order; A03 points fill forward."""
    try:
        root = ET.fromstring(payload)
        for element in root.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        if root.tag == "Acknowledgement_MarketDocument":
            raise SourceError("ENTSO-E returned no load forecast for this day.")
        step = timedelta(minutes=15)
        values = {}
        for series in root.findall("TimeSeries"):
            if series.findtext("outBiddingZone_Domain.mRID") != AREA:
                raise SourceError("Unexpected bidding zone.")
            if series.findtext("quantity_Measure_Unit.name") != "MAW":
                raise SourceError("Unexpected load units.")
            curve = series.findtext("curveType", "A01")
            if curve not in ("A01", "A03"):
                raise SourceError("Unsupported curve type; preserve raw response for audit.")
            for period in series.findall("Period"):
                if period.findtext("resolution") != "PT15M":
                    raise SourceError("Unsupported load forecast resolution.")
                start = datetime.fromisoformat(
                    period.findtext("timeInterval/start").replace("Z", "+00:00")
                )
                end = datetime.fromisoformat(
                    period.findtext("timeInterval/end").replace("Z", "+00:00")
                )
                count = (end - start) // step
                points = sorted(
                    (int(p.findtext("position")), float(p.findtext("quantity")))
                    for p in period.findall("Point")
                )
                positions = [position for position, _ in points]
                if (
                    not points
                    or positions[0] != 1
                    or len(set(positions)) != len(positions)
                    or positions[-1] > count
                    or (curve == "A01" and positions != list(range(1, count + 1)))
                ):
                    raise SourceError("Missing or duplicate interval positions.")
                for index, (position, value) in enumerate(points):
                    if not math.isfinite(value):
                        raise SourceError("Nonfinite load forecast.")
                    stop = points[index + 1][0] if index + 1 < len(points) else count + 1
                    for slot in range(position, stop):
                        interval = start + (slot - 1) * step
                        if values.setdefault(interval, value) != value:
                            raise SourceError("Conflicting load forecast intervals.")
        grid = delivery_grid(day)
        if any(t not in values for t in grid):
            raise SourceError("Load forecast does not cover the delivery day.")
        return [values[t] for t in grid]
    except SourceError:
        raise
    except Exception:
        raise SourceError("Malformed or unsupported ENTSO-E load forecast XML.") from None


def ingest_load_days(first: date, last: date, key: str, data_dir: Path) -> dict[date, dict]:
    """Archive and normalize delivery days FIRST to LAST (inclusive) from one request.

    Cached days are not refetched. Each day is checked on its own, so the result maps
    a day to its checkpoint record or to the SourceError that left it missing.
    """
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    checkpoints = {day: data_dir / "load" / "days" / f"{day}.json" for day in days}
    results = {
        day: json.loads(path.read_text()) | {"cached": True}
        for day, path in checkpoints.items()
        if path.exists()
    }
    pending = [day for day in days if day not in results]
    if not pending:
        return results
    start, end = day_bounds(pending[0])[0], day_bounds(pending[-1])[1]
    payload = fetch_document(key, QUERY, start, end)
    raw_digest = hashlib.sha256(payload).hexdigest()
    raw = data_dir / "load" / "raw" / f"{raw_digest}.xml"
    if not raw.exists():
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(payload)
    retrieved_at = datetime.now(UTC).isoformat()
    for day in pending:
        try:
            values = parse_load(payload, day)
        except SourceError as error:
            results[day] = error
            continue
        normalized = {
            "schema_version": 1,
            "delivery_date": str(day),
            "document": "A65/A01",
            "retrieved_at": retrieved_at,
            "raw_sha256": raw_digest,
            "start": day_bounds(day)[0].isoformat(),
            "minutes": 15,
            "mw": values,
        }
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        relative = Path("load") / "versions" / f"{digest}.json"
        atomic_json(data_dir / relative, normalized)
        record = {"path": str(relative), "sha256": digest, "delivery_date": str(day)}
        atomic_json(checkpoints[day], record)
        results[day] = record | {"cached": False}
    return results


def load_load(data_dir: Path) -> dict[date, dict]:
    """Every archived delivery day, checksum-verified: {day: normalized forecast}."""
    archive = {}
    for path in sorted((data_dir / "load" / "days").glob("*.json")):
        record = json.loads(path.read_text())
        value = json.loads((data_dir / record["path"]).read_text())
        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        if digest != record["sha256"]:
            raise SourceError(f"Load forecast checksum mismatch for {record['delivery_date']}.")
        archive[date.fromisoformat(record["delivery_date"])] = value
    return archive
