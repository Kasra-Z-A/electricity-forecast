import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from power_forecast.baseline import forecast
from power_forecast.cli import app
from power_forecast.history import atomic_json, ingest_day, load_version
from power_forecast.ingestion.entsoe import AREA, SourceError
from power_forecast.timegrid import day_bounds, delivery_grid, forecast_cutoff

WEEKLY = "seasonal-weekly-v1"


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


def test_resume_no_requests_and_tamper_detection(tmp_path):
    day = date(2026, 9, 7)
    record = seed(tmp_path, day)
    with patch("power_forecast.history.fetch_prices") as fetch:
        assert ingest_day(day, "test", tmp_path)["cached"]
        fetch.assert_not_called()
    path = tmp_path / record["path"]
    content = json.loads(path.read_text())
    content["prices"][0]["eur_mwh"] = 12345
    atomic_json(path, content)
    with pytest.raises(SourceError, match="checksum"):
        load_version(tmp_path, record)


def test_failed_day_does_not_checkpoint_and_resume(tmp_path):
    runner = CliRunner()
    with (
        patch("power_forecast.cli.api_key", return_value="test"),
        patch("time.sleep"),
        patch(
            "power_forecast.history.fetch_prices",
            side_effect=[payload(date(2026, 9, 1)), SourceError("outage")],
        ),
    ):
        result = runner.invoke(
            app, ["backfill-prices", "2026-09-01", "2026-09-03", "--data-dir", str(tmp_path)]
        )
    assert result.exit_code == 1
    assert (tmp_path / "prices/days/2026-09-01.json").exists()
    assert not (tmp_path / "prices/days/2026-09-02.json").exists()
    with (
        patch("power_forecast.cli.api_key", return_value="test"),
        patch("time.sleep"),
        patch(
            "power_forecast.history.fetch_prices", return_value=payload(date(2026, 9, 2))
        ) as fetch,
    ):
        result = runner.invoke(
            app, ["backfill-prices", "2026-09-01", "2026-09-03", "--data-dir", str(tmp_path)]
        )
    assert result.exit_code == 0, result.output
    assert fetch.call_count == 1


def test_weekly_replay_and_future_information_rejected(tmp_path):
    seed(tmp_path, date(2026, 9, 6))
    day = date(2026, 9, 13)
    now = forecast_cutoff(day) + timedelta(minutes=5)
    with pytest.raises(SourceError, match="No eligible"):
        forecast(day, tmp_path, now=now, model=WEEKLY)
    output = forecast(day, tmp_path, replay=True, now=now, model=WEEKLY)
    value = json.loads(output.read_text())
    assert value["mode"] == "reconstructed_replay"
    assert [p["point"] for p in value["predictions"]] == list(range(1, 97))
    assert not any(p["fallback"] for p in value["predictions"])
    assert forecast(day, tmp_path, replay=True, now=now, model=WEEKLY) == output


def test_dst_local_mapping_and_missing_spring_slot(tmp_path):
    seed(tmp_path, date(2026, 3, 29))  # prior week has no 02:xx
    seed(tmp_path, date(2026, 4, 4))
    value = json.loads(forecast(date(2026, 4, 5), tmp_path, replay=True, model=WEEKLY).read_text())
    fallback = [p for p in value["predictions"] if p["fallback"]]
    assert len(fallback) == 4
    assert all(p["lag_days"] == 1 for p in fallback)


def test_autumn_fold_and_hourly_transition(tmp_path):
    seed(tmp_path, date(2025, 10, 19))
    value = json.loads(
        forecast(date(2025, 10, 26), tmp_path, replay=True, model=WEEKLY).read_text()
    )
    assert len(value["predictions"]) == 100
    assert [p["point"] for p in value["predictions"]][8:16] == [9, 10, 11, 12] * 2
    seed(tmp_path, date(2025, 9, 24))
    value = json.loads(forecast(date(2025, 10, 1), tmp_path, replay=True, model=WEEKLY).read_text())
    assert len(value["predictions"]) == 96
    assert all(p["hourly_derived"] for p in value["predictions"])
    assert [p["point"] for p in value["predictions"]][:4] == [1] * 4


def test_no_history_no_forecast_file(tmp_path):
    with pytest.raises(SourceError):
        forecast(date(2026, 9, 13), tmp_path, replay=True)
    assert not (tmp_path / "forecasts").exists()


def test_live_early_run_rejected(tmp_path):
    with pytest.raises(SourceError, match="not arrived"):
        forecast(date(2026, 9, 15), tmp_path, now=datetime(2026, 9, 14, 7, tzinfo=UTC))


def test_scoring_pending_idempotent_and_pinned(tmp_path):
    from power_forecast.scoring import score

    seed(tmp_path, date(2026, 9, 6))
    run = forecast(date(2026, 9, 13), tmp_path, replay=True)
    with pytest.raises(SourceError, match="pending"):
        score(run, tmp_path)
    seed(tmp_path, date(2026, 9, 13))
    output = score(run, tmp_path)
    assert output == score(run, tmp_path)
    result = json.loads(output.read_text())
    assert result["mae"] == result["rmse"] == 0
    assert result["intervals"] == 96
    value = json.loads(run.read_text())
    value["predictions"][0]["point"] = 99999
    atomic_json(run, value)
    with pytest.raises(SourceError, match="checksum"):
        score(run, tmp_path)


def test_snapshot_selection_respects_retrieval_cutoff(tmp_path):
    seed(tmp_path, date(2026, 9, 8))
    day = date(2026, 9, 15)
    from power_forecast.history import historical_versions

    snapshots = historical_versions(tmp_path, day, datetime(2100, 1, 1, tzinfo=UTC), False)
    assert len(snapshots) == 1
    # Verify frozen version selection using the actual recorded retrieval time.
    retrieved = datetime.fromisoformat(snapshots[0][0]["retrieved_at"])
    assert historical_versions(tmp_path, day, retrieved - timedelta(seconds=1), False) == []
    assert len(historical_versions(tmp_path, day, retrieved, False)) == 1


def test_scoring_nonzero_errors(tmp_path):
    from power_forecast.scoring import score

    seed(tmp_path, date(2026, 9, 6))
    run = forecast(date(2026, 9, 13), tmp_path, replay=True)
    raw = payload(date(2026, 9, 13))
    from defusedxml import ElementTree as ET

    root = ET.fromstring(raw)
    for element in root.iter("price.amount"):
        element.text = str(float(element.text) + 10)
    with patch("power_forecast.history.fetch_prices", return_value=ET.tostring(root)):
        ingest_day(date(2026, 9, 13), "test", tmp_path)
    result = json.loads(score(run, tmp_path).read_text())
    assert result["mae"] == result["rmse"] == 10


def window_payload(days):
    # One document holding a TimeSeries per day, as ENTSO-E returns for a long window.
    parts = [payload(day).decode() for day in days]
    series = [p[p.index("<TimeSeries>") : p.index("</Publication_MarketDocument>")] for p in parts]
    head = parts[0][: parts[0].index("<TimeSeries>")]
    return (head + "".join(series) + "</Publication_MarketDocument>").encode()


def test_window_ingestion_matches_per_day_ingestion(tmp_path):
    from power_forecast.history import ingest_window

    days = [date(2025, 9, 29) + timedelta(days=n) for n in range(4)]  # spans 60 -> 15 min
    for day in days:
        with patch("power_forecast.history.fetch_prices", return_value=payload(day)):
            ingest_day(day, "test", tmp_path / "daily")
    with patch("power_forecast.history.fetch_prices", return_value=window_payload(days)):
        stored = ingest_window(days, "test", tmp_path / "window")
        assert len(stored) == 4
        assert ingest_window(days, "test", tmp_path / "window") == []
    for day in days:
        records = [
            json.loads((tmp_path / kind / "prices/days" / f"{day}.json").read_text())
            for kind in ("daily", "window")
        ]
        daily, window = (
            load_version(tmp_path / kind, record)[1]
            for kind, record in zip(("daily", "window"), records)
        )
        assert daily == window


def test_invalid_window_stores_nothing(tmp_path):
    from power_forecast.history import ingest_window

    days = [date(2026, 9, 1) + timedelta(days=n) for n in range(3)]
    broken = window_payload(days[:2])  # third day missing from the response
    with patch("power_forecast.history.fetch_prices", return_value=broken):
        with pytest.raises(SourceError, match="2026-09-03"):
            ingest_window(days, "test", tmp_path)
    assert not (tmp_path / "prices").exists() and not (tmp_path / "raw").exists()
