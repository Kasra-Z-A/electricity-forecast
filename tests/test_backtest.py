import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from test_history_baseline import payload

from power_forecast.backtest import backtest, load_archive, predict, trailing_high_price
from power_forecast.baseline import PRODUCTION_MODEL, forecast, slot_lookup
from power_forecast.history import ingest_day
from power_forecast.metrics import paired_comparison, summarize


def distinct_payload(day):
    # Every day gets its own prices so a wrong lag cannot match by accident.
    marker = f"<price.amount>{day.toordinal() % 997}".encode()
    return payload(day).replace(b"<price.amount>", marker)


def seed_days(tmp_path, first, last):
    day = first
    while day <= last:
        with patch("power_forecast.history.fetch_prices", return_value=distinct_payload(day)):
            ingest_day(day, "test", tmp_path)
        day += timedelta(days=1)


@pytest.mark.parametrize("target", [date(2026, 3, 29), date(2025, 10, 27), date(2026, 9, 17)])
@pytest.mark.parametrize(
    "strategy, model", [("weekly", "seasonal-weekly-v1"), ("prior_day", "seasonal-daily-v1")]
)
def test_backtest_matches_production_forecast(tmp_path, target, strategy, model):
    seed_days(tmp_path, target - timedelta(days=28), target - timedelta(days=1))
    run = json.loads(forecast(target, tmp_path, replay=True, model=model).read_text())
    assert run["model"] == model
    archive = load_archive(tmp_path)
    points = predict(strategy, slot_lookup(archive.values()), target)
    assert [p["point"] for p in points] == [p["point"] for p in run["predictions"]]
    assert [p["fallback"] for p in points] == [p["fallback"] for p in run["predictions"]]


def test_unavailable_results_cannot_enter_a_forecast(tmp_path):
    target = date(2026, 9, 17)
    seed_days(tmp_path, target - timedelta(days=28), target + timedelta(days=3))
    archive = load_archive(tmp_path)
    lookup = slot_lookup(archive.values())
    with_future = predict("prior_day", lookup, target)
    without = {k: v for k, v in lookup.items() if k[0] < target}
    assert with_future == predict("prior_day", without, target)
    previous = [row.eur_mwh for row in archive[target - timedelta(days=1)][1]]
    assert [p["point"] for p in with_future] == previous
    past = {day: value for day, value in archive.items() if day < target}
    assert trailing_high_price(archive, target) == trailing_high_price(past, target)


def test_weekday_median_and_unavailable_days(tmp_path):
    target = date(2026, 9, 17)
    seed_days(tmp_path, target - timedelta(days=28), target - timedelta(days=1))
    archive = load_archive(tmp_path)
    points = predict("weekday_median", slot_lookup(archive.values()), target)
    assert not any(p["fallback"] for p in points)
    assert predict("weekly", {}, target) is None
    seed_days(tmp_path, target, target)
    result = backtest(load_archive(tmp_path), [target], ["weekly", "weekday_median"])
    assert all(len(r[0]["intervals"]) == 96 for r in result.values())


def test_paired_comparison_and_summary(tmp_path):
    target = date(2026, 9, 17)
    seed_days(tmp_path, target - timedelta(days=42), target + timedelta(days=13))
    days = [target + timedelta(days=n) for n in range(14)]
    result = backtest(load_archive(tmp_path), days, ["weekly", "prior_day"])
    same = paired_comparison(result["weekly"], result["weekly"])
    assert same["mae_skill"] == 0 and same["difference_95"] == [0, 0]
    assert same["common_days"] == 14 and same["week_blocks"] == 3
    other = paired_comparison(result["prior_day"], result["weekly"])
    assert other == paired_comparison(result["prior_day"], result["weekly"])
    low, high = other["difference_95"]
    assert low <= other["daily_mae_difference"] <= high
    summary = summarize(result["weekly"], high_price=dict.fromkeys(days, 1e9))
    assert summary["days"] == 14 and summary["unavailable_days"] == 0
    assert summary["intervals"] == 14 * 96
    assert summary["slices"]["high_price"]["intervals"] == 0
    assert set(summary["by_weekday"]) == {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}


def test_production_publishes_the_daily_baseline():
    assert PRODUCTION_MODEL == "seasonal-daily-v1"
