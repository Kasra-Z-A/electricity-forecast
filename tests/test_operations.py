import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from test_history_baseline import payload, seed
from typer.testing import CliRunner

from power_forecast.cli import app
from power_forecast.ingestion.entsoe import SourceError
from power_forecast.operations import health, run_job, writer_lock
from power_forecast.timegrid import forecast_cutoff

DAY = date(2026, 9, 17)
CUTOFF = forecast_cutoff(DAY)


def eligible_history(tmp_path):
    with patch("power_forecast.history.datetime", wraps=datetime) as clock:
        clock.now.return_value = CUTOFF - timedelta(minutes=20)
        seed(tmp_path, DAY - timedelta(days=7))


def test_outage_recovery_publication_and_scoring(tmp_path):
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF):
        with pytest.raises(SourceError, match="No eligible"):
            run_job("publish", tmp_path, DAY)
        failure = json.loads((tmp_path / "operations/publish.json").read_text())
        assert failure["status"] == "failed"
        assert not (tmp_path / "official").exists()
        eligible_history(tmp_path)
        first = run_job("publish", tmp_path, DAY)
        again = run_job("publish", tmp_path, DAY)
    assert first["result"]["forecast_path"] == again["result"]["forecast_path"]
    assert again["result"]["reused"]
    assert len(list((tmp_path / "forecasts").glob("*.json"))) == 1
    with (
        patch("power_forecast.operations.api_key", return_value="test"),
        patch("power_forecast.operations.sleep"),
        patch("power_forecast.history.fetch_prices", return_value=payload(DAY)),
    ):
        scored = run_job("settle", tmp_path, DAY)
        repeated = run_job("settle", tmp_path, DAY)
    assert scored["result"]["results"][0]["status"] == "scored"
    assert repeated["result"]["results"][0]["status"] == "scored"
    assert len(list((tmp_path / "operations/attempts").glob("*.json"))) == 5


@pytest.mark.parametrize("minutes", [-1, 31])
def test_publication_window(tmp_path, minutes):
    with patch(
        "power_forecast.operations.utcnow", return_value=CUTOFF + timedelta(minutes=minutes)
    ):
        with pytest.raises(SourceError, match="Publication is allowed"):
            run_job("publish", tmp_path, DAY)
    assert not (tmp_path / "official").exists()


def test_late_publication_and_watchdog(tmp_path):
    eligible_history(tmp_path)
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF + timedelta(minutes=16)):
        assert "missing_forecast" in health(DAY, tmp_path)["issues"]
        result = run_job("publish", tmp_path, DAY)
        assert result["result"]["status"] == "late"
        check = run_job("watchdog", tmp_path, DAY)
        assert check["status"] == "unhealthy"
        assert "late_forecast" in check["result"]["issues"]


def test_concurrent_writers_refused_and_lock_released(tmp_path):
    with writer_lock(tmp_path):
        with pytest.raises(SourceError, match="Another writer"):
            run_job("collect", tmp_path, DAY)
        result = CliRunner().invoke(app, ["ingest-prices", str(DAY), "--data-dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "Another writer" in result.output
    with writer_lock(tmp_path):
        pass


def test_collection_partial_outage_does_not_discard_other_days(tmp_path):
    with (
        patch("power_forecast.operations.api_key", return_value="test"),
        patch("power_forecast.operations.sleep"),
        patch(
            "power_forecast.operations.ingest_day",
            side_effect=[SourceError("secret-containing upstream error"), *[{"cached": True}] * 27],
        ),
    ):
        result = run_job("collect", tmp_path, DAY)
    assert result["status"] == "partial"
    assert len(result["result"]["completed"]) == 27
    assert "secret-containing" not in json.dumps(result)


def test_post_cutoff_collection_cannot_enter_official_forecast(tmp_path):
    with patch("power_forecast.history.datetime", wraps=datetime) as clock:
        clock.now.return_value = CUTOFF + timedelta(seconds=1)
        seed(tmp_path, DAY - timedelta(days=7))
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF + timedelta(minutes=5)):
        with pytest.raises(SourceError, match="No eligible"):
            run_job("publish", tmp_path, DAY)


def test_default_delivery_day_uses_vienna_not_utc(tmp_path):
    with patch(
        "power_forecast.operations.utcnow", return_value=datetime(2026, 9, 15, 22, 30, tzinfo=UTC)
    ):
        result = run_job("watchdog", tmp_path)
    assert result["delivery_date"] == "2026-09-17"


def test_killed_attempt_remains_visible(tmp_path):
    with patch("power_forecast.operations.collect", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run_job("collect", tmp_path, DAY)
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF):
        assert "collection_failed_or_incomplete" in health(DAY, tmp_path)["issues"]


def test_watchdog_reports_unscored_forecast(tmp_path):
    eligible_history(tmp_path)
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF):
        run_job("publish", tmp_path, DAY)
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF + timedelta(hours=5)):
        assert "pending_scores" in health(DAY, tmp_path)["issues"]


def test_publication_deadline_uses_completion_time(tmp_path):
    eligible_history(tmp_path)
    from power_forecast.operations import publish

    with patch(
        "power_forecast.operations.utcnow",
        side_effect=[CUTOFF + timedelta(minutes=14), CUTOFF + timedelta(minutes=16)],
    ):
        with writer_lock(tmp_path):
            assert publish(DAY, tmp_path)["status"] == "late"


def test_pending_scoring_retries_after_source_recovers(tmp_path):
    eligible_history(tmp_path)
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF):
        run_job("publish", tmp_path, DAY)
    with (
        patch("power_forecast.operations.api_key", return_value="test"),
        patch("power_forecast.operations.sleep"),
        patch("power_forecast.history.fetch_prices", side_effect=SourceError("No data")),
    ):
        assert run_job("settle", tmp_path, DAY)["status"] == "partial"
    with (
        patch("power_forecast.operations.api_key", return_value="test"),
        patch("power_forecast.operations.sleep"),
        patch("power_forecast.history.fetch_prices", return_value=payload(DAY)),
    ):
        assert run_job("settle", tmp_path, DAY)["status"] == "success"


def test_settle_distinguishes_unpublished_results_from_failures(tmp_path):
    eligible_history(tmp_path)
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF):
        run_job("publish", tmp_path, DAY)

    def settle_at(hours, **source):
        with (
            patch("power_forecast.operations.utcnow", return_value=CUTOFF + timedelta(hours=hours)),
            patch("power_forecast.operations.api_key", return_value="test"),
            patch("power_forecast.operations.sleep"),
            patch("power_forecast.history.fetch_prices", **source),
        ):
            return run_job("settle", tmp_path, DAY)

    unpublished = {"side_effect": SourceError("No data")}
    early = settle_at(2.5, **unpublished)
    assert early["status"] == "success"
    assert early["result"]["results"][0]["status"] == "awaiting_results"
    overdue = settle_at(5.5, **unpublished)
    assert overdue["status"] == "partial"
    assert overdue["result"]["results"][0]["status"] == "pending_or_failed"
    assert settle_at(5.5, return_value=payload(DAY))["status"] == "success"
    refresh_failed = settle_at(9.5, **unpublished)
    assert refresh_failed["status"] == "success"
    assert refresh_failed["result"]["results"][0]["status"] == "kept_previous_score"


def test_failed_observation_never_changes_collection_status(tmp_path, no_live_observations):
    no_live_observations.side_effect = RuntimeError("observation crashed")
    with (
        patch("power_forecast.operations.api_key", return_value="test"),
        patch("power_forecast.operations.sleep"),
        patch("power_forecast.operations.ingest_day", return_value={"cached": True}),
    ):
        result = run_job("collect", tmp_path, DAY)
    assert result["status"] == "success"
    assert result["result"]["observations"] == [
        {"status": "error", "error_type": "RuntimeError"},
        {"document": "weather", "status": "cached"},
    ]


def test_load_forecast_is_archived_without_affecting_collection_status(tmp_path):
    from power_forecast.ingestion.entsoe import SourceError

    stubs = (
        patch("power_forecast.operations.api_key", return_value="test"),
        patch("power_forecast.operations.sleep"),
        patch("power_forecast.operations.ingest_day", return_value={"cached": True}),
    )
    outcomes = [
        ({DAY: {"cached": False}}, {"status": "archived"}),
        ({DAY: SourceError("gap")}, {"status": "error", "error_type": "SourceError"}),
        (RuntimeError("down"), {"status": "error", "error_type": "RuntimeError"}),
    ]
    for outcome, expected in outcomes:
        stored = (
            {"side_effect": outcome}
            if isinstance(outcome, Exception)
            else {"return_value": outcome}
        )
        with (
            stubs[0],
            stubs[1],
            stubs[2],
            patch("power_forecast.operations.ingest_load_days", **stored) as ingest,
        ):
            result = run_job("collect", tmp_path, DAY)
        assert ingest.call_args.args[:2] == (DAY, DAY)
        assert result["status"] == "success"
        assert result["result"]["load_forecast"] == expected
