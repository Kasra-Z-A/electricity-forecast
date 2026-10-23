from datetime import date
from unittest.mock import patch

import pytest
from test_backtest import seed_days

from power_forecast.backtest import load_archive


@pytest.fixture
def archive(tmp_path):
    """Synthetic native-era archive, 2026-07-10 through 2026-09-11."""
    seed_days(tmp_path, date(2026, 7, 10), date(2026, 9, 11))
    return load_archive(tmp_path)


@pytest.fixture(autouse=True)
def no_live_observations():
    """collect also logs covariate availability, archives the load forecast and fills
    training history; tests must never reach ENTSO-E or Open-Meteo."""
    with (
        patch("power_forecast.operations.observe", return_value=[]) as observe,
        patch("power_forecast.operations.observe_weather", return_value={"status": "cached"}),
        patch("power_forecast.operations.ingest_window", return_value=[]),
        patch("power_forecast.operations.ingest_load_days", return_value={}),
    ):
        yield observe
