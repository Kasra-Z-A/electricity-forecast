import json
from datetime import date
from unittest.mock import patch

from power_forecast.coverage import coverage, expected_intervals
from power_forecast.history import ingest_day
from power_forecast.ingestion.entsoe import AREA
from power_forecast.timegrid import day_bounds, delivery_grid


def payload(day):
    start, end = day_bounds(day)
    minutes = 15 if day >= date(2025, 10, 1) else 60
    points = "".join(
        f"<Point><position>{n}</position><price.amount>{n}</price.amount></Point>"
        for n in range(1, len(delivery_grid(day, minutes)) + 1)
    )
    return f"""<Publication_MarketDocument><mRID>test</mRID><revisionNumber>1</revisionNumber>
    <TimeSeries><currency_Unit.name>EUR</currency_Unit.name><price_Measure_Unit.name>MWH</price_Measure_Unit.name>
    <in_Domain.mRID>{AREA}</in_Domain.mRID><out_Domain.mRID>{AREA}</out_Domain.mRID><curveType>A01</curveType>
    <Period><timeInterval><start>{start.isoformat()}</start><end>{end.isoformat()}</end></timeInterval>
    <resolution>PT{minutes}M</resolution>{points}</Period></TimeSeries></Publication_MarketDocument>""".encode()


def seed(tmp_path, day):
    with patch("power_forecast.history.fetch_prices", return_value=payload(day)):
        return ingest_day(day, "test", tmp_path)


def test_expected_intervals_come_from_the_grid_not_constants():
    assert expected_intervals(date(2026, 3, 29)) == 92
    assert expected_intervals(date(2025, 10, 26)) == 100
    assert expected_intervals(date(2026, 9, 14)) == 96
    assert expected_intervals(date(2019, 3, 31)) == 23
    assert expected_intervals(date(2019, 10, 27)) == 25
    assert expected_intervals(date(2019, 7, 1)) == 24


def test_eras_split_at_the_resolution_transition(tmp_path):
    for day in (date(2025, 9, 29), date(2025, 9, 30), date(2025, 10, 1), date(2025, 10, 2)):
        seed(tmp_path, day)
    report = coverage(tmp_path, verify=True)
    assert report["days"] == 4
    assert report["missing_days"] == 0
    assert report["eras"]["hourly"] == {
        "days": 2,
        "resolution_minutes": 60,
        "first": "2025-09-29",
        "last": "2025-09-30",
    }
    assert report["eras"]["native"]["days"] == 2
    assert report["interval_counts"] == {"24": 2, "96": 2}
    assert report["complete"]


def test_missing_days_are_grouped_into_gaps(tmp_path):
    seed(tmp_path, date(2026, 9, 1))
    seed(tmp_path, date(2026, 9, 4))
    report = coverage(tmp_path)
    assert report["missing_days"] == 2
    assert report["gaps"] == [{"first": "2026-09-02", "last": "2026-09-03", "days": 2}]
    assert not report["complete"]


def test_checkpoint_inconsistent_with_the_contract_is_reported(tmp_path):
    seed(tmp_path, date(2026, 9, 1))
    path = tmp_path / "prices/days/2026-09-01.json"
    record = json.loads(path.read_text())
    record["intervals"] = 95
    path.write_text(json.dumps(record))
    report = coverage(tmp_path)
    assert report["interval_mismatches"] == [{"day": "2026-09-01", "expected": 96, "actual": 95}]
    assert not report["complete"]


def test_verify_detects_a_tampered_snapshot(tmp_path):
    record = seed(tmp_path, date(2026, 9, 1))
    version = tmp_path / record["path"]
    value = json.loads(version.read_text())
    value["prices"][0]["eur_mwh"] = 999.0
    version.write_text(json.dumps(value))
    assert coverage(tmp_path)["complete"] is True
    report = coverage(tmp_path, verify=True)
    assert [f["day"] for f in report["checksum_failures"]] == ["2026-09-01"]
    assert not report["complete"]
