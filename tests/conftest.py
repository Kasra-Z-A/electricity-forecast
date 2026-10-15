from datetime import date

import pytest
from test_backtest import seed_days

from power_forecast.backtest import load_archive


@pytest.fixture
def archive(tmp_path):
    """Synthetic native-era archive, 2026-07-10 through 2026-09-11."""
    seed_days(tmp_path, date(2026, 7, 10), date(2026, 9, 11))
    return load_archive(tmp_path)
