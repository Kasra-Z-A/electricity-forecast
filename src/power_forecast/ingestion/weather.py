"""Archived ECMWF IFS single runs from Open-Meteo, one 00 UTC run per delivery day.

The run initialized at 00 UTC on D-1 is typically available four to six hours later,
before the 10:30 Vienna cutoff; historical runs are reconstructed, not verified
vintages (see docs/SOURCE_AUDIT.md). Data: Open-Meteo.com, CC BY 4.0.
"""

import hashlib
import json
import math
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from power_forecast.history import atomic_json
from power_forecast.ingestion.entsoe import SourceError

ENDPOINT = "https://single-runs-api.open-meteo.com/v1/forecast"
MODEL = "ecmwf_ifs"
# Regional proxies from PLAN.md section 5, not a capacity-weighted grid.
LOCATIONS = {
    "vienna": (48.21, 16.37),
    "graz": (47.07, 15.44),
    "linz": (48.31, 14.29),
    "salzburg": (47.81, 13.04),
    "innsbruck": (47.27, 11.39),
    "eisenstadt": (47.85, 16.52),
}
VARIABLES = ("temperature_2m", "wind_speed_100m", "shortwave_radiation")
FIRST_RUN = date(2024, 3, 14)


def run_for(day: date) -> datetime:
    """The single run a forecast for delivery `day` may use: 00 UTC on D-1."""
    return datetime.combine(day - timedelta(days=1), datetime.min.time(), UTC)


def fetch_run(run: datetime) -> bytes:
    params = {
        "latitude": ",".join(str(lat) for lat, _ in LOCATIONS.values()),
        "longitude": ",".join(str(lon) for _, lon in LOCATIONS.values()),
        "run": run.strftime("%Y-%m-%dT%H:%M"),
        "hourly": ",".join(VARIABLES),
        "models": MODEL,
        "forecast_hours": 48,
        "timezone": "GMT",
    }
    for attempt in range(3):
        try:
            response = httpx.get(ENDPOINT, params=params, timeout=60)
        except httpx.HTTPError:
            if attempt == 2:
                raise SourceError("Open-Meteo connection failed after 3 attempts.") from None
        else:
            if response.status_code == 200:
                return response.content
            if response.status_code not in (429, 500, 502, 503, 504):
                raise SourceError(f"Open-Meteo request failed (HTTP {response.status_code}).")
            if attempt == 2:
                raise SourceError("Open-Meteo temporarily unavailable after 3 attempts.")
        time.sleep(2**attempt)
    raise SourceError("Open-Meteo request failed.")


def parse_run(payload: bytes, run: datetime) -> dict[str, dict[str, list[float]]]:
    """Per location and variable, 48 hourly values starting at the run time."""
    try:
        locations = json.loads(payload)
    except ValueError:
        raise SourceError("Malformed Open-Meteo response.") from None
    if not isinstance(locations, list) or len(locations) != len(LOCATIONS):
        raise SourceError("Open-Meteo response does not cover every location.")
    expected = [(run + timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M") for n in range(48)]
    parsed = {}
    for name, value in zip(LOCATIONS, locations, strict=True):
        hourly = value.get("hourly", {})
        if hourly.get("time") != expected:
            raise SourceError(f"Unexpected hourly grid for {name}.")
        series = {}
        for variable in VARIABLES:
            values = hourly.get(variable)
            # Radiation averages the preceding hour, so the run's first step has none.
            # That hour precedes the delivery day and is never used.
            checked = values[1:] if variable == "shortwave_radiation" and values else values
            if not values or any(v is None or not math.isfinite(v) for v in checked):
                raise SourceError(f"Missing {variable} for {name}.")
            series[variable] = [None if v is None else float(v) for v in values]
        parsed[name] = series
    return parsed


def ingest_weather_day(day: date, data_dir: Path) -> dict:
    """Archive and normalize the run for delivery `day`; cached days are not refetched."""
    checkpoint = data_dir / "weather" / "days" / f"{day}.json"
    if checkpoint.exists():
        return json.loads(checkpoint.read_text()) | {"cached": True}
    run = run_for(day)
    if run.date() < FIRST_RUN:
        raise SourceError(f"No archived single run before {FIRST_RUN}.")
    payload = fetch_run(run)
    values = parse_run(payload, run)
    raw_digest = hashlib.sha256(payload).hexdigest()
    raw = data_dir / "weather" / "raw" / f"{raw_digest}.json"
    if not raw.exists():
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(payload)
    normalized = {
        "schema_version": 1,
        "delivery_date": str(day),
        "model": MODEL,
        "run": run.isoformat(),
        "retrieved_at": datetime.now(UTC).isoformat(),
        "raw_sha256": raw_digest,
        "locations": {name: list(coords) for name, coords in LOCATIONS.items()},
        "values": values,
    }
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    relative = Path("weather") / "versions" / f"{digest}.json"
    atomic_json(data_dir / relative, normalized)
    record = {
        "path": str(relative),
        "sha256": digest,
        "delivery_date": str(day),
        "run": run.isoformat(),
    }
    atomic_json(checkpoint, record)
    return record | {"cached": False}


def load_weather(data_dir: Path) -> dict[date, dict]:
    """Every archived delivery day, checksum-verified: {day: normalized run}."""
    archive = {}
    for path in sorted((data_dir / "weather" / "days").glob("*.json")):
        record = json.loads(path.read_text())
        value = json.loads((data_dir / record["path"]).read_text())
        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        if digest != record["sha256"]:
            raise SourceError(f"Weather checksum mismatch for {record['delivery_date']}.")
        archive[date.fromisoformat(record["delivery_date"])] = value
    return archive
