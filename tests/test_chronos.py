from datetime import date, timedelta

import pytest
from test_backtest import seed_days
from test_lear import covariates

from power_forecast.backtest import load_archive
from power_forecast.timegrid import day_bounds

pytest.importorskip("pandas")

from power_forecast.chronos import frames  # noqa: E402

DAY = date(2026, 9, 17)


def test_context_ends_where_the_delivery_day_begins(tmp_path):
    seed_days(tmp_path, DAY - timedelta(days=20), DAY)
    archive = load_archive(tmp_path)
    extra = covariates(DAY - timedelta(days=20), DAY)
    context, future = frames(archive, extra["load"], extra["weather"], DAY)
    start = day_bounds(DAY)[0].replace(tzinfo=None)
    assert context["timestamp"].max() == start - timedelta(minutes=15)
    assert future["timestamp"].min() == start and len(future) == 96
    assert "target" not in future and context["timestamp"].is_monotonic_increasing
    # The delivery day's own prices are never in the context.
    assert len(context) == 20 * 96


def test_context_stops_at_the_newest_gap(tmp_path):
    seed_days(tmp_path, DAY - timedelta(days=20), DAY)
    archive = load_archive(tmp_path)
    extra = covariates(DAY - timedelta(days=20), DAY)
    del archive[DAY - timedelta(days=9)]
    context, _ = frames(archive, extra["load"], extra["weather"], DAY)
    assert len(context) == 8 * 96
    del archive[DAY - timedelta(days=3)]
    assert frames(archive, extra["load"], extra["weather"], DAY) is None
