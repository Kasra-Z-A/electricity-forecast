import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from test_lifecycle import CUTOFF, DAY, seed_at

from power_forecast.backtest import load_archive
from power_forecast.baseline import PRODUCTION_MODEL
from power_forecast.history import atomic_json
from power_forecast.ingestion.entsoe import SourceError
from power_forecast.lifecycle import CANDIDATE, pointer_path
from power_forecast.operations import run_job
from power_forecast.promotion import WEEKLY, champion, events, promote, rollback, run_gate

NOW = CUTOFF + timedelta(days=1)


def place(data_dir, model, day, actual, error, mode="live"):
    """A shadow forecast whose every interval misses the actual by `error`."""
    run = {
        "model": model,
        "delivery_date": str(day),
        "issued_at": NOW.isoformat(),
        "predictions": [
            {"start": row.start.isoformat(), "point": row.eur_mwh + error} for row in actual
        ],
    }
    run["run_id"] = hashlib.sha256(json.dumps(run, sort_keys=True).encode()).hexdigest()
    path = data_dir / "shadow" / "forecasts" / f"{run['run_id']}.json"
    atomic_json(path, run)
    record = {"model": model, "delivery_date": str(day), "mode": mode}
    record["forecast_path"] = str(path.relative_to(data_dir))
    atomic_json(pointer_path(data_dir, model, day), record)


@pytest.fixture
def scored(tmp_path):
    """35 days of actuals, each forecast by the candidate, champion and weekly baseline."""
    first = DAY - timedelta(days=35)
    seed_at(tmp_path, first, DAY - timedelta(days=1), CUTOFF - timedelta(days=40))
    archive = load_archive(tmp_path)

    def forecasts(candidate_error, days=35, live=20, champion_error=5.0):
        for n in range(days):
            day = first + timedelta(days=n)
            rows = archive[day][1]
            # Alternating signs keep daily differences varied for the bootstrap.
            sign = 1 if n % 2 else -1
            mode = "live" if n >= days - live else "vintage_replay"
            place(tmp_path, CANDIDATE["name"], day, rows, sign * candidate_error, mode)
            place(tmp_path, PRODUCTION_MODEL, day, rows, sign * champion_error)
            place(tmp_path, WEEKLY, day, rows, sign * 8.0)

    return tmp_path, forecasts


def test_better_candidate_is_promoted_automatically(scored):
    data_dir, forecasts = scored
    forecasts(candidate_error=4.0)
    result = run_gate(data_dir, NOW)
    assert result["status"] == "promoted", result
    assert champion(data_dir)["model"] == CANDIDATE["name"]
    assert champion(data_dir)["previous"] == {"model": PRODUCTION_MODEL}
    [event] = events(data_dir)
    assert (event["event"], event["evidence"]) == ("promote", result["evaluation"])
    assert run_gate(data_dir, NOW)["status"] == "candidate_is_champion"


@pytest.mark.parametrize(
    "kwargs, failed",
    [
        ({"candidate_error": 4.0, "days": 20, "live": 20}, "common_days"),
        ({"candidate_error": 4.0, "live": 10}, "live_shadow_days"),
        ({"candidate_error": 4.95}, "mae_improvement"),
        ({"candidate_error": 9.0, "champion_error": 10.0}, "beats_weekly"),
    ],
)
def test_candidate_is_kept_out_unless_every_check_passes(scored, kwargs, failed):
    data_dir, forecasts = scored
    forecasts(**kwargs)
    result = run_gate(data_dir, NOW)
    assert result["status"] == "kept_champion"
    assert failed in result["failed_checks"]
    assert champion(data_dir)["model"] == PRODUCTION_MODEL
    assert events(data_dir) == []


def test_rollback_restores_the_baseline_and_blocks_the_candidate(scored):
    data_dir, forecasts = scored
    forecasts(candidate_error=4.0)
    run_gate(data_dir, NOW)
    event = rollback(data_dir, NOW, "drill")
    assert (event["model"], event["restored"]) == (CANDIDATE["name"], PRODUCTION_MODEL)
    assert champion(data_dir) == {"model": PRODUCTION_MODEL}
    again = run_gate(data_dir, NOW)
    assert again["failed_checks"] == ["not_rolled_back"]
    with pytest.raises(SourceError, match="no previous champion"):
        rollback(data_dir, NOW, "nothing to restore")


def test_missing_champion_forecasts_are_replayed_from_vintage_versions(tmp_path):
    seed_at(tmp_path, DAY - timedelta(days=40), DAY, CUTOFF - timedelta(days=60))
    rows = load_archive(tmp_path)[DAY][1]
    place(tmp_path, CANDIDATE["name"], DAY, rows, 1.0)
    report = run_gate(tmp_path, NOW)
    assert report["common_days"] == 1
    for model in (PRODUCTION_MODEL, WEEKLY):
        pointer = json.loads(pointer_path(tmp_path, model, DAY).read_text())
        run = json.loads((tmp_path / pointer["forecast_path"]).read_text())
        assert (run["mode"], run["model"]) == ("vintage_replay", model)


@pytest.fixture
def promoted(tmp_path):
    seed_at(
        tmp_path, DAY - timedelta(days=60), DAY - timedelta(days=1), CUTOFF - timedelta(hours=1)
    )
    promote(tmp_path, CANDIDATE["name"], "test-evidence", NOW)
    return tmp_path


def test_champion_candidate_publishes_officially_without_a_shadow(promoted):
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF + timedelta(minutes=1)):
        result = run_job("publish", promoted, DAY)["result"]
    run = json.loads((promoted / result["forecast_path"]).read_text())
    assert (run["model"], run["role"], run["mode"]) == (CANDIDATE["name"], "official", "live")
    assert result["shadow"]["status"] == "skipped"
    assert "fallback_from" not in result


def test_failing_champion_falls_back_to_the_seasonal_baseline(promoted):
    with (
        patch("power_forecast.operations.utcnow", return_value=CUTOFF + timedelta(minutes=1)),
        patch("power_forecast.operations.candidate_forecast", side_effect=ValueError("broken")),
    ):
        result = run_job("publish", promoted, DAY)
    assert result["status"] == "success"
    record = result["result"]
    run = json.loads((promoted / record["forecast_path"]).read_text())
    assert run["model"] == PRODUCTION_MODEL
    assert record["fallback_from"] == {"model": CANDIDATE["name"], "error_type": "ValueError"}


def test_monitoring_compares_models_on_the_same_recent_days(scored):
    from power_forecast.promotion import monitoring

    data_dir, forecasts = scored
    forecasts(candidate_error=4.0, live=10)
    run_gate(data_dir, NOW)
    summary = monitoring(data_dir)
    rows = {row["model"]: row for row in summary["rows"]}
    assert rows[CANDIDATE["name"]]["role"] == "shadow"
    assert rows[PRODUCTION_MODEL]["role"] == "published"
    assert rows[CANDIDATE["name"]]["mae_7"] == pytest.approx(4.0)
    assert rows[WEEKLY]["mae_28"] == pytest.approx(8.0)
