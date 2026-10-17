import json
from datetime import UTC, date, datetime, timedelta

import pytest
from test_backtest import seed_days

from power_forecast.backtest import load_archive
from power_forecast.baseline import slot_lookup
from power_forecast.features import day_features, feature_names
from power_forecast.ingestion.entsoe import SourceError
from power_forecast.ingestion.weather import LOCATIONS, VARIABLES, parse_run, run_for

DAY = date(2026, 9, 17)


def response(run, radiation_first=None, hole=None):
    hours = [(run + timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M") for n in range(48)]
    locations = []
    for index, _ in enumerate(LOCATIONS):
        hourly = {"time": hours}
        for variable in VARIABLES:
            hourly[variable] = [float(index * 100 + n) for n in range(48)]
        hourly["shortwave_radiation"][0] = radiation_first
        if hole is not None:
            hourly["temperature_2m"][hole] = None
        locations.append({"hourly": hourly})
    return json.dumps(locations).encode()


def normalized(run):
    return {"run": run.isoformat(), "values": parse_run(response(run), run)}


def test_parse_accepts_only_the_first_radiation_gap():
    run = run_for(DAY)
    assert run == datetime(2026, 9, 16, tzinfo=UTC)
    values = parse_run(response(run), run)
    assert values["vienna"]["shortwave_radiation"][0] is None
    with pytest.raises(SourceError, match="temperature_2m"):
        parse_run(response(run, hole=30), run)
    with pytest.raises(SourceError, match="hourly grid"):
        parse_run(response(run + timedelta(hours=6)), run)


def test_weather_features_use_the_run_of_the_day_before(tmp_path):
    seed_days(tmp_path, DAY - timedelta(days=21), DAY - timedelta(days=1))
    archive = load_archive(tmp_path)
    lookup = slot_lookup(archive.values())
    names = feature_names(("weather",))
    rows = day_features(archive, lookup, DAY, {"weather": {DAY: normalized(run_for(DAY))}})
    assert len(rows) == 96 and all(len(row) == len(names) for row in rows)
    # Vienna midnight on DAY is 22:00 UTC on D-1, step 22 of the run; six locations
    # hold n, 100 + n, ..., 500 + n, so their mean is 250 + n.
    first, last = rows[0], rows[-1]
    assert first[names.index("wx_temperature")] == 250 + 22
    assert last[names.index("wx_temperature")] == 250 + 45
    assert len({row[names.index("wx_solar_day")] for row in rows}) == 1
    assert day_features(archive, lookup, DAY, {"weather": {}}) is None
    later = {DAY: normalized(run_for(DAY) + timedelta(days=1))}
    with pytest.raises(ValueError, match="00 UTC run"):
        day_features(archive, lookup, DAY, {"weather": later})
