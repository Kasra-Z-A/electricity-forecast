import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from test_history_baseline import seed
from test_operations import CUTOFF, DAY, eligible_history

from power_forecast.baseline import forecast
from power_forecast.dashboard import build_dashboard, dashboard_data
from power_forecast.history import atomic_json
from power_forecast.operations import run_job


def test_empty_archive_exports_complete_site(tmp_path):
    output = build_dashboard(tmp_path)
    assert (output / "index.html").is_file()
    assert (output / "fonts/instrument-serif-latin-400-normal.woff2").is_file()
    data = json.loads((output / "dashboard.json").read_text())
    assert data["curves"] == []
    assert not data["tomorrow_available"]
    assert all(j["status"] == "not_started" for j in data["jobs"])


def test_official_metrics_and_public_export(tmp_path):
    eligible_history(tmp_path)
    forecast(DAY, tmp_path, replay=True)  # replays are never shown
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF):
        run_job("publish", tmp_path, DAY)
    seed(tmp_path, DAY)
    data = dashboard_data(tmp_path, CUTOFF)
    assert len(data["curves"]) == 1
    curve = data["curves"][0]
    assert curve["status"] == "on_time"
    assert curve["mae"] is not None and len(curve["points"]) == 96
    assert "raw_sha256" not in json.dumps(data)
    assert "forecast_path" not in json.dumps(data)


def test_only_official_live_forecasts_and_freshness(tmp_path):
    eligible_history(tmp_path)
    forecast(DAY, tmp_path, now=CUTOFF)  # Diagnostic only.
    assert dashboard_data(tmp_path, CUTOFF)["curves"] == []
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF):
        run_job("publish", tmp_path, DAY)
    data = dashboard_data(tmp_path, CUTOFF)
    assert data["tomorrow_available"]
    assert len(data["curves"]) == 1
    assert data["curves"][0]["mae"] is None
    assert not dashboard_data(tmp_path, CUTOFF + timedelta(days=1))["tomorrow_available"]


def test_corruption_preserves_previous_export(tmp_path):
    eligible_history(tmp_path)
    path = forecast(DAY, tmp_path, replay=True)
    output = build_dashboard(tmp_path)
    previous = (output / "dashboard.json").read_bytes()
    value = json.loads(path.read_text())
    value["predictions"][0]["point"] = 123456
    atomic_json(path, value)
    with pytest.raises(ValueError, match="checksum"):
        build_dashboard(tmp_path)
    assert (output / "dashboard.json").read_bytes() == previous


def test_export_job_and_private_error_details(tmp_path):
    atomic_json(
        tmp_path / "operations/collect.json",
        {"status": "failed", "error": "PRIVATE", "result": {"token": "PRIVATE"}},
    )
    run_job("dashboard", tmp_path)
    assert "PRIVATE" not in (tmp_path / "dashboard/dashboard.json").read_text()
    with pytest.raises(ValueError, match="separate"):
        build_dashboard(tmp_path, tmp_path)


def test_scheduler_status_and_next_cutoff(tmp_path):
    atomic_json(
        tmp_path / "operations/scheduler.json",
        {"active": True, "checked_at": CUTOFF.isoformat(), "private": "not exported"},
    )
    before = dashboard_data(tmp_path, CUTOFF - timedelta(minutes=1))
    after = dashboard_data(tmp_path, CUTOFF + timedelta(minutes=1))
    assert before["scheduler_active"]
    assert before["next_forecast_at"] == CUTOFF.isoformat()
    assert before["next_delivery_date"] == str(DAY)
    assert after["next_delivery_date"] == str(DAY + timedelta(days=1))
    assert "not exported" not in json.dumps(after)
