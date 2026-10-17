import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from test_backtest import distinct_payload

from power_forecast.backtest import load_archive
from power_forecast.history import ingest_day
from power_forecast.lifecycle import CANDIDATE, current_fit, shadow_forecast
from power_forecast.models import FeatureCache, build
from power_forecast.operations import run_job
from power_forecast.timegrid import forecast_cutoff

DAY = date(2026, 9, 17)
CUTOFF = forecast_cutoff(DAY)


def seed_at(tmp_path, first, last, retrieved_at, revised=False):
    """Ingest days as if retrieved at `retrieved_at`; `revised` changes every price."""
    day = first
    while day <= last:
        body = distinct_payload(day)
        if revised:
            body = body.replace(b"<price.amount>", b"<price.amount>9")
        with (
            patch("power_forecast.history.fetch_prices", return_value=body),
            patch("power_forecast.history.datetime", wraps=datetime) as clock,
        ):
            clock.now.return_value = retrieved_at
            ingest_day(day, "test", tmp_path, refresh=True)
        day += timedelta(days=1)


@pytest.fixture
def history(tmp_path):
    seed_at(
        tmp_path, DAY - timedelta(days=60), DAY - timedelta(days=1), CUTOFF - timedelta(hours=1)
    )
    return tmp_path


def test_candidate_matches_the_frozen_config():
    frozen = json.loads(Path("configs/candidate.json").read_text())
    assert CANDIDATE == {k: v for k, v in frozen.items() if k != "_comment"}


def test_shadow_forecast_matches_the_research_pipeline(history):
    record = shadow_forecast(DAY, history, CUTOFF)
    run = json.loads((history / record["forecast_path"]).read_text())
    archive = load_archive(history)
    cache = FeatureCache(archive)
    x, y = cache.matrix(DAY - timedelta(days=180), DAY - timedelta(days=1))
    expected = build("ridge", CANDIDATE["params"], 0).fit(x, y).predict(np.array(cache(DAY)))
    points = [p["point"] for p in run["predictions"]]
    np.testing.assert_allclose(points, expected, rtol=1e-7, atol=1e-7)
    assert (run["role"], run["mode"], run["training_labels"]) == ("shadow", "live", "vintage")


def test_versions_retrieved_after_the_cutoff_are_ignored(history, tmp_path_factory):
    clean = shadow_forecast(DAY, history, CUTOFF)
    revised = tmp_path_factory.mktemp("revised")
    seed_at(revised, DAY - timedelta(days=60), DAY - timedelta(days=1), CUTOFF - timedelta(hours=1))
    seed_at(
        revised,
        DAY - timedelta(days=3),
        DAY - timedelta(days=1),
        CUTOFF + timedelta(minutes=5),
        True,
    )
    late = shadow_forecast(DAY, revised, CUTOFF + timedelta(minutes=10))
    points = [
        [p["point"] for p in json.loads((root / r["forecast_path"]).read_text())["predictions"]]
        for root, r in ((history, clean), (revised, late))
    ]
    assert points[0] == points[1]


def test_fits_are_reused_for_a_week_then_refreshed(history):
    first = current_fit(history, CUTOFF)
    assert current_fit(history, CUTOFF + timedelta(days=6))["fit_id"] == first["fit_id"]
    seed_at(history, DAY, DAY + timedelta(days=6), CUTOFF + timedelta(days=6))
    later = current_fit(history, CUTOFF + timedelta(days=7))
    assert later["fit_id"] != first["fit_id"]
    assert later["train_last"] == str(DAY + timedelta(days=6))


def test_shadow_runs_once_after_publication_and_is_scored(history):
    with patch("power_forecast.operations.utcnow", return_value=CUTOFF + timedelta(minutes=1)):
        first = run_job("publish", history, DAY)["result"]
        again = run_job("publish", history, DAY)["result"]
    assert (first["shadow"]["status"], again["shadow"]["status"]) == ("saved", "reused")
    assert len(list((history / "forecasts").glob("*.json"))) == 1  # only the official run
    shadow = list((history / "shadow" / "forecasts").glob("*.json"))
    assert len(shadow) == 1
    with (
        patch("power_forecast.operations.api_key", return_value="test"),
        patch("power_forecast.operations.sleep"),
        patch("power_forecast.history.fetch_prices", return_value=distinct_payload(DAY)),
    ):
        settled = run_job("settle", history, DAY)["result"]
    assert settled["shadow"] == [{"model": CANDIDATE["name"], "day": str(DAY), "status": "scored"}]


def test_a_failing_shadow_never_fails_publication(history):
    with (
        patch("power_forecast.operations.utcnow", return_value=CUTOFF + timedelta(minutes=1)),
        patch("power_forecast.operations.shadow_forecast", side_effect=RuntimeError("boom")),
    ):
        result = run_job("publish", history, DAY)
    assert result["status"] == "success"
    assert result["result"]["shadow"] == {"status": "error", "error_type": "RuntimeError"}


def test_collect_fills_only_missing_training_history(history):
    from power_forecast.operations import fill_training_history

    calls = []

    def window(days, key, data_dir):
        calls.append(days)
        return days

    with patch("power_forecast.operations.ingest_window", side_effect=window):
        with patch("power_forecast.operations.sleep"):
            result = fill_training_history(DAY, "test", history)
    requested = [day for chunk in calls for day in chunk]
    assert requested[0] == DAY - timedelta(days=CANDIDATE["training_window"] + 15)
    assert requested[-1] == DAY - timedelta(days=61)  # seeded days are not refetched
    assert all(len(chunk) <= 31 for chunk in calls)
    assert result == {"missing": len(requested), "downloaded": len(requested), "status": "success"}


def test_replay_uses_vintage_inputs_and_marks_reconstructed_labels(history):
    from typer.testing import CliRunner

    from power_forecast.cli import app

    # Labels retrieved long after the cutoff, as the host's backfill will be.
    seed_at(
        history, DAY - timedelta(days=90), DAY - timedelta(days=61), CUTOFF + timedelta(days=30)
    )
    end = str(DAY + timedelta(days=1))
    outcome = CliRunner().invoke(app, ["shadow-replay", str(DAY), end, "--data-dir", str(history)])
    assert outcome.exit_code == 0, outcome.output
    pointer = json.loads((history / "shadow/days" / CANDIDATE["name"] / f"{DAY}.json").read_text())
    run = json.loads((history / pointer["forecast_path"]).read_text())
    assert (run["mode"], run["training_labels"]) == ("vintage_replay", "reconstructed")
    fit = json.loads((history / "models/fits" / f"{run['fit_id']}.json").read_text())
    # Labels retrieved after the cutoff count; the earliest day has no lag history.
    assert fit["training_days"] == 89
    assert all(s["retrieved_at"] < CUTOFF.isoformat() for s in run["input_snapshots"])
