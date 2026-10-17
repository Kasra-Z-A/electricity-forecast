"""Prospective availability evidence for candidate covariates (SOURCE_AUDIT.md).

ENTSO-E responses carry no publication time; `createdDateTime` is when the response
was generated. The only evidence that a forecast for D existed before the cutoff is
having retrieved it then, so the 10:00 collection records what each document held
at that moment in an append-only log. Nothing here feeds a forecast.
"""

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

from defusedxml import ElementTree as ET

from power_forecast.ingestion.entsoe import AREA, SourceError, fetch_document
from power_forecast.timegrid import day_bounds

DOCUMENTS = {
    # Regulation 543/2013 Art. 6(2)(b): due two hours before day-ahead gate closure.
    "load_forecast": {"documentType": "A65", "processType": "A01", "outBiddingZone_Domain": AREA},
    # Art. 14(2)(d): due by 18:00 Brussels time on D-1, after our cutoff.
    "wind_solar_forecast": {"documentType": "A69", "processType": "A01", "in_Domain": AREA},
}


def describe(payload: bytes, day: date) -> dict:
    """Status and series shape of one response; `complete` means every series covers D."""
    root = ET.fromstring(payload)
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    if root.tag == "Acknowledgement_MarketDocument":
        return {"status": "missing", "series": []}
    start, end = (t.strftime("%Y-%m-%dT%H:%MZ") for t in day_bounds(day))
    series = []
    for item in root.findall("TimeSeries"):
        for period in item.findall("Period"):
            series.append(
                {
                    "psr_type": item.findtext("MktPSRType/psrType"),
                    "curve": item.findtext("curveType", "A01"),
                    "resolution": period.findtext("resolution"),
                    "start": period.findtext("timeInterval/start"),
                    "end": period.findtext("timeInterval/end"),
                    "points": len(period.findall("Point")),
                }
            )
    covered = bool(series) and all(s["start"] <= start and s["end"] >= end for s in series)
    return {"status": "complete" if covered else "incomplete", "series": series}


def observe(day: date, key: str, data_dir: Path) -> list[dict]:
    """Fetch each candidate document for delivery `day` and log what was there."""
    directory = data_dir / "observations"
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, query in DOCUMENTS.items():
        start, end = day_bounds(day)
        retrieved_at = datetime.now(UTC).isoformat()
        try:
            payload = fetch_document(key, query, start, end)
            raw = directory / "raw" / f"{hashlib.sha256(payload).hexdigest()}.xml"
            raw.parent.mkdir(parents=True, exist_ok=True)
            if not raw.exists():
                raw.write_bytes(payload)
            entry = describe(payload, day) | {"raw_sha256": raw.stem}
        except (SourceError, ET.ParseError) as error:
            entry = {"status": "error", "error_type": type(error).__name__, "series": []}
        entry = {"document": name, "delivery_date": str(day), "retrieved_at": retrieved_at} | entry
        with (directory / f"{name}.jsonl").open("a") as log:
            log.write(json.dumps(entry) + "\n")
        entries.append(entry)
    return entries
