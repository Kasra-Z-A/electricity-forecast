from datetime import date, timedelta

import pytest
from test_backtest import seed_days

from power_forecast.backtest import load_archive
from power_forecast.ingestion.entsoe import Price
from power_forecast.models import FeatureCache, backtest_model

SPLITS = {
    "refit_days": 7,
    "eras": {
        "native": {
            "train": ["2026-08-01", "2026-08-28"],
            "validation": ["2026-08-29", "2026-09-11"],
            "holdout": ["2026-09-12", "2026-09-12"],
        }
    },
}
CONFIG = {
    "seed": 0,
    "models": {
        "ridge": {"alpha": 1.0},
        "lightgbm": {"objective": "l1", "n_estimators": 20, "min_child_samples": 20},
    },
}


@pytest.fixture
def archive(tmp_path):
    seed_days(tmp_path, date(2026, 7, 10), date(2026, 9, 11))
    return load_archive(tmp_path)


def run(archive, kind, window="expanding"):
    return backtest_model(
        FeatureCache(archive), SPLITS, "native", "validation", window, kind, CONFIG
    )


def points(results):
    return [i["error"] + i["actual"] for day in results for i in day["intervals"]]


@pytest.mark.parametrize("kind", ["ridge", "lightgbm"])
def test_last_evaluated_result_cannot_change_any_forecast(archive, kind):
    original = run(archive, kind)
    assert [d["day"] for d in original][0] == date(2026, 8, 29)
    assert len(original) == 14
    last = date(2026, 9, 11)
    record, rows = archive[last]
    shocked = [Price(r.start, r.minutes, r.eur_mwh + 1000, r.document_id, r.revision) for r in rows]
    changed = run(archive | {last: (record, shocked)}, kind)
    assert points(changed) == pytest.approx(points(original))


@pytest.mark.parametrize("kind", ["ridge", "lightgbm"])
def test_fits_are_deterministic(archive, kind):
    assert points(run(archive, kind, 14)) == points(run(archive, kind, 14))


def test_training_matrix_stays_inside_its_window(archive):
    cache = FeatureCache(archive)
    features, targets = cache.matrix(date(2026, 8, 1), date(2026, 8, 7))
    assert features.shape == (7 * 96, 17) and targets.shape == (7 * 96,)
    assert set(cache.rows) == {date(2026, 8, 1) + timedelta(days=n) for n in range(7)}
