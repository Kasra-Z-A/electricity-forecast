import json
from datetime import timedelta
from unittest.mock import patch

import numpy as np
import pytest
from test_lear import covariates
from test_lifecycle import CUTOFF, DAY, seed_at

pytest.importorskip("lightgbm")

from power_forecast import challengers  # noqa: E402
from power_forecast.lifecycle import CHALLENGERS, pointer_path  # noqa: E402
from power_forecast.timegrid import delivery_grid, forecast_cutoff  # noqa: E402

FIRST = DAY - timedelta(days=60)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """60 days of prices, weather and load, retrieved before the weekly fit and the cutoff."""
    seed_at(tmp_path, FIRST, DAY - timedelta(days=1), CUTOFF - timedelta(days=10))
    values = covariates(FIRST, DAY)
    retrieved = {name: CUTOFF - timedelta(hours=1) for name in values}

    def loader(name):
        def load(data_dir):
            return {
                day: value | {"retrieved_at": retrieved[name].isoformat()}
                for day, value in values[name].items()
            }

        return load

    monkeypatch.setattr(challengers, "load_weather", loader("weather"))
    monkeypatch.setattr(challengers, "load_load", loader("load"))
    monkeypatch.setattr(challengers, "REPLAY_DAYS", 2)

    def fake_chronos(self, days):
        # Stands in for the Chronos-2 subprocess, which needs the pinned weights.
        self.chronos |= {
            day: np.array([[40.0, 50.0, 60.0]] * len(delivery_grid(day))) for day in days
        }

    monkeypatch.setattr(challengers.Inputs, "run_chronos", fake_chronos)
    return tmp_path, retrieved


def read(data_dir, model, day):
    pointer = json.loads(pointer_path(data_dir, model, day).read_text())
    return pointer, json.loads((data_dir / pointer["forecast_path"]).read_text())


def test_fits_change_once_a_week():
    monday = challengers.ANCHOR + timedelta(weeks=40)
    assert challengers.anchor(monday) == forecast_cutoff(monday)
    assert challengers.anchor(monday + timedelta(days=6)) == forecast_cutoff(monday)
    assert challengers.anchor(monday + timedelta(days=7)) == forecast_cutoff(
        monday + timedelta(days=7)
    )


def test_challengers_forecast_live_and_replay_recent_days(archive):
    data_dir, _ = archive
    report = challengers.run_challengers(
        DAY, data_dir, CUTOFF + timedelta(minutes=40), "seasonal-daily-v1"
    )
    assert report["status"] == "success", report
    for model in CHALLENGERS:
        pointer, run = read(data_dir, model, DAY)
        assert (pointer["mode"], run["role"], run["mode"]) == ("live", "shadow", "live")
        assert run["covariates"] == "retrieved_before_cutoff"
        assert len(run["predictions"]) == 96
        replay, _ = read(data_dir, model, DAY - timedelta(days=1))
        assert replay["mode"] == "vintage_replay"
    # Shadow forecasts never reach the official archive.
    assert not (data_dir / "forecasts").exists() and not (data_dir / "official").exists()
    again = challengers.run_challengers(
        DAY, data_dir, CUTOFF + timedelta(hours=1), "seasonal-daily-v1"
    )
    assert all(outcome == {} for outcome in again["models"].values())


def test_a_champion_challenger_publishes_officially_and_leaves_the_job(archive):
    data_dir, _ = archive
    model = next(iter(CHALLENGERS))
    path = challengers.official_forecast(model, DAY, data_dir, CUTOFF + timedelta(minutes=1))
    run = json.loads(path.read_text())
    assert path.parent == data_dir / "forecasts"
    assert (run["model"], run["role"], run["status"]) == (model, "official", "on_time")
    report = challengers.run_challengers(DAY, data_dir, CUTOFF + timedelta(minutes=40), model)
    assert model not in report["models"]


def test_inputs_retrieved_after_the_cutoff_block_only_the_live_forecast(archive):
    data_dir, retrieved = archive
    retrieved["load"] = CUTOFF + timedelta(minutes=5)
    report = challengers.run_challengers(
        DAY, data_dir, CUTOFF + timedelta(minutes=40), "seasonal-daily-v1"
    )
    assert report["status"] == "partial"
    for model in CHALLENGERS:
        assert report["models"][model][str(DAY)]["status"] == "error"
        assert not pointer_path(data_dir, model, DAY).exists()
        _, replay = read(data_dir, model, DAY - timedelta(days=1))
        assert replay["covariates"] == "reconstructed"
        zero_shot = CHALLENGERS[model]["kind"] == "chronos"
        assert replay["training_labels"] == ("none" if zero_shot else "reconstructed")


def test_challengers_appear_as_shadow_rows(archive):
    from power_forecast.promotion import monitoring

    data_dir, _ = archive
    summary = monitoring(data_dir)
    roles = {row["model"]: row["role"] for row in summary["rows"]}
    assert {roles[model] for model in CHALLENGERS} == {"shadow"}
    assert set(CHALLENGERS) <= set(summary["catalog"])


def test_publish_runs_a_promoted_challenger(archive):
    from power_forecast.operations import run_job
    from power_forecast.promotion import promote

    data_dir, _ = archive
    model = next(iter(CHALLENGERS))
    promote(data_dir, model, "test-evidence", CUTOFF)
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF + timedelta(minutes=1)):
        record = run_job("publish", data_dir, DAY)["result"]
    assert "fallback_from" not in record
    assert json.loads((data_dir / record["forecast_path"]).read_text())["model"] == model


def test_chronos_reports_its_band_and_falls_back_to_lear_when_published(archive):
    from power_forecast.operations import run_job
    from power_forecast.promotion import promote

    data_dir, _ = archive
    challengers.run_challengers(DAY, data_dir, CUTOFF + timedelta(minutes=40), "x")
    _, run = read(data_dir, "chronos2-zs-v1", DAY)
    assert run["training_labels"] == "none" and run["fit"]["revision"]
    assert {(p["p10"], p["point"], p["p90"]) for p in run["predictions"]} == {(40.0, 50.0, 60.0)}

    promote(data_dir, "chronos2-zs-v1", "test-evidence", CUTOFF)
    with (
        patch("power_forecast.operations.utcnow", return_value=CUTOFF + timedelta(minutes=1)),
        patch.object(challengers.Inputs, "run_chronos", side_effect=RuntimeError("no weights")),
    ):
        record = run_job("publish", data_dir, DAY + timedelta(days=0))["result"]
    published = json.loads((data_dir / record["forecast_path"]).read_text())
    assert published["model"] == "lear-wl-v1"
    assert record["fallback_from"] == {"model": "chronos2-zs-v1", "error_type": "RuntimeError"}
