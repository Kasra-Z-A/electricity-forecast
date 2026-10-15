"""Small raw ENTSO-E adapter; never expose token-bearing transport errors."""

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from defusedxml import ElementTree as ET

AREAS = {"AT": "10YAT-APG------L", "DE-LU": "10Y1001A1001A82H"}
AREA = AREAS["AT"]
ENDPOINT = "https://web-api.tp.entsoe.eu/api"


class SourceError(Exception):
    """Safe diagnostic suitable for user-facing output."""


@dataclass(frozen=True)
class Price:
    start: datetime
    minutes: int
    eur_mwh: float
    document_id: str
    revision: str


def parse_prices(payload: bytes, area: str = AREA) -> list[Price]:
    import math

    try:
        root = ET.fromstring(payload)
        # Strip namespace only after the hardened parser has rejected unsafe XML.
        for element in root.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        if root.tag == "Acknowledgement_MarketDocument":
            raise SourceError("ENTSO-E returned an acknowledgement/no-data response.")
        document_id = root.findtext("mRID", "")
        revision = root.findtext("revisionNumber", "")
        prices = []
        for series in root.findall("TimeSeries"):
            sequence = series.findtext("classificationSequence_AttributeInstanceComponent.position")
            if sequence is not None and sequence != "1":
                continue  # SDAC sequence 1; separate auctions must not be mixed.
            if (
                series.findtext("currency_Unit.name") != "EUR"
                or series.findtext("price_Measure_Unit.name") != "MWH"
            ):
                raise SourceError("Unexpected price units.")
            for tag in ("in_Domain.mRID", "out_Domain.mRID"):
                if series.findtext(tag) != area:
                    raise SourceError("Unexpected bidding zone.")
            curve = series.findtext("curveType", "A01")
            if curve not in ("A01", "A03"):
                raise SourceError("Unsupported curve type; preserve raw response for audit.")
            for period in series.findall("Period"):
                resolution = period.findtext("resolution")
                if resolution not in ("PT15M", "PT60M"):
                    raise SourceError("Unsupported price resolution.")
                minutes = 15 if resolution == "PT15M" else 60
                start = datetime.fromisoformat(
                    period.findtext("timeInterval/start").replace("Z", "+00:00")
                )
                end = datetime.fromisoformat(
                    period.findtext("timeInterval/end").replace("Z", "+00:00")
                )
                if (
                    start.utcoffset() != timedelta(0)
                    or end.utcoffset() != timedelta(0)
                    or end <= start
                ):
                    raise SourceError("Invalid UTC period bounds.")
                step = timedelta(minutes=minutes)
                if (end - start) % step:
                    raise SourceError("Misaligned period bounds.")
                points = period.findall("Point")
                positions = [int(p.findtext("position")) for p in points]
                count = (end - start) // step
                if (
                    not positions
                    or len(set(positions)) != len(positions)
                    or min(positions) != 1
                    or max(positions) > count
                ):
                    raise SourceError("Missing or duplicate interval positions.")
                if curve == "A01" and sorted(positions) != list(range(1, count + 1)):
                    raise SourceError("Missing or duplicate interval positions.")
                ordered = sorted(zip(positions, points))
                expanded = []
                for index, (position, point) in enumerate(ordered):
                    stop = ordered[index + 1][0] if index + 1 < len(ordered) else count + 1
                    expanded.extend((point, slot) for slot in range(position, stop))
                for point, position in expanded:
                    value = float(point.findtext("price.amount"))
                    if not math.isfinite(value):
                        raise SourceError("Nonfinite price.")
                    prices.append(
                        Price(
                            start + (position - 1) * step,
                            minutes,
                            value,
                            document_id,
                            revision,
                        )
                    )
        if not prices:
            raise SourceError("No price intervals in response.")
        prices = list(dict.fromkeys(prices))  # Collapse only identical records, never conflicts.
        prices.sort(key=lambda row: row.start)
        for left, right in zip(prices, prices[1:]):
            if left.start + timedelta(minutes=left.minutes) > right.start:
                raise SourceError("Overlapping price series require source audit.")
        return prices
    except SourceError:
        raise
    except Exception:
        raise SourceError("Malformed or unsupported ENTSO-E price XML.") from None


def fetch_prices(key: str, start: datetime, end: datetime, area: str = AREA) -> bytes:
    params = {
        "securityToken": key,
        "documentType": "A44",
        "in_Domain": area,
        "out_Domain": area,
        "periodStart": start.strftime("%Y%m%d%H%M"),
        "periodEnd": end.strftime("%Y%m%d%H%M"),
    }
    for attempt in range(3):
        try:
            response = httpx.get(ENDPOINT, params=params, timeout=45)
        except httpx.HTTPError:
            if attempt == 2:
                raise SourceError("ENTSO-E connection failed after 3 attempts.") from None
        else:
            if response.status_code == 200:
                return response.content
            if response.status_code not in (429, 500, 502, 503, 504):
                raise SourceError(f"ENTSO-E request failed (HTTP {response.status_code}).")
            if attempt == 2:
                raise SourceError("ENTSO-E temporarily unavailable after 3 attempts.")
        time.sleep(2**attempt)
    raise SourceError("ENTSO-E request failed.")


def archive(
    payload: bytes, directory: Path, start: datetime, end: datetime, area: str = AREA
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()
    target = directory / f"{digest}.xml"
    try:
        with target.open("xb") as file:
            file.write(payload)
    except FileExistsError:
        if target.read_bytes() != payload:
            raise SourceError("Archive integrity check failed.")
    record = {
        "sha256": digest,
        "file": target.name,
        "area": area,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "retrieved_at": datetime.now(UTC).isoformat(),
    }
    with (directory / "manifest.jsonl").open("a") as file:
        file.write(json.dumps(record) + "\n")
    return target


def serialize(rows: list[Price]) -> str:
    return (
        json.dumps([asdict(row) | {"start": row.start.isoformat()} for row in rows], indent=2)
        + "\n"
    )
