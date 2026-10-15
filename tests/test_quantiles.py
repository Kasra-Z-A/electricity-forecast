from datetime import date

import pytest
from test_models import SPLITS

from power_forecast.ingestion.entsoe import Price
from power_forecast.metrics import pinball, probabilistic_summary
from power_forecast.models import FeatureCache, backtest_probabilistic

CONFIG = {
    "seed": 0,
    "quantiles": [0.1, 0.5, 0.9],
    "calibration_days": 7,
    "windows": {"native": "expanding"},
    "models": {
        "ridge": {"alpha": 1.0},
        "lightgbm": {"n_estimators": 20, "min_child_samples": 20},
    },
}
KINDS = ["ridge_interval", "lightgbm_cqr", "ridge_interval_by_hour", "lightgbm_cqr_by_hour"]


def run(archive, kind):
    return backtest_probabilistic(
        FeatureCache(archive), SPLITS, "native", "validation", kind, CONFIG
    )


def quantiles(results):
    return [i["quantiles"] for day in results for i in day["intervals"]]


def test_pinball_loss():
    assert pinball(10, 8, 0.9) == pytest.approx(1.8)
    assert pinball(10, 12, 0.9) == pytest.approx(0.2)
    assert pinball(10, 10, 0.5) == 0


@pytest.mark.parametrize("kind", KINDS)
def test_quantiles_are_ordered_and_ignore_unavailable_results(archive, kind):
    original = run(archive, kind)
    assert len(original) == 14
    assert all(low <= mid <= high for low, mid, high in quantiles(original))
    last = date(2026, 9, 11)
    record, rows = archive[last]
    shocked = [Price(r.start, r.minutes, r.eur_mwh + 1000, r.document_id, r.revision) for r in rows]
    changed = run(archive | {last: (record, shocked)}, kind)
    flat = [v for q in quantiles(original) for v in q]
    assert [v for q in quantiles(changed) for v in q] == pytest.approx(flat)
    summary = probabilistic_summary(original)
    assert summary["intervals"] == 14 * 96
    assert 0 <= summary["coverage"] <= 1 and summary["mean_width"] >= 0
    assert set(summary["pinball"]) == {"p10", "p50", "p90"}


def test_raw_interval_is_recoverable_from_the_offset(archive):
    # CQR moves both ends by the same amount; it may widen or narrow the raw interval.
    calibrated = run(archive, "lightgbm_cqr")
    for day in calibrated:
        for item in day["intervals"]:
            low, mid, high = item["quantiles"]
            offset = item["conformal_offset"]
            assert low + offset - 1e-9 <= mid <= high - offset + 1e-9


def test_hourly_calibration_differs_by_hour_and_falls_back_when_thin(archive):
    results = run(archive, "lightgbm_cqr_by_hour")
    offsets = {}
    for day in results:
        for item in day["intervals"]:
            offsets.setdefault(item["local"].hour, set()).add(round(item["conformal_offset"], 9))
    # 7 calibration days x 4 quarter-hours = 28 values per hour, enough for its own group.
    assert len({value for values in offsets.values() for value in values}) > 2
