from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from test_backtest import seed_days

from power_forecast.backtest import load_archive
from power_forecast.splits import last_label_day
from power_forecast.timegrid import delivery_grid

pytest.importorskip("sklearn")

from power_forecast.lear import LearInputs, fit_window, local_curve, predict_window  # noqa: E402

DAY = date(2026, 9, 17)


def weather_run(day):
    run = datetime.combine(day - timedelta(days=1), datetime.min.time(), UTC)
    hourly = [float(n) for n in range(48)]
    places = {"vienna": {v: hourly for v in ("temperature_2m", "wind_speed_100m")}}
    places["vienna"]["shortwave_radiation"] = [None, *hourly[1:]]
    return {"run": run.isoformat(), "values": places}


def covariates(first, last):
    load, weather = {}, {}
    day = first
    while day <= last:
        load[day] = {"mw": [5000.0 + n for n in range(len(delivery_grid(day)))]}
        weather[day] = weather_run(day)
        day += timedelta(days=1)
    return {"load": load, "weather": weather}


def test_local_curve_maps_dst_days_onto_96_clock_slots():
    spring = local_curve(date(2026, 3, 29), [float(n) for n in range(92)])
    # 01:45 is position 7; 02:00-02:45 does not exist and repeats 01:00-01:45.
    assert len(spring) == 96 and spring[8:12] == spring[4:8] and spring[12] == 8.0
    autumn = local_curve(date(2025, 10, 26), [float(n) for n in range(100)])
    # Both folds of 02:00 are averaged: positions 8 and 12 of the UTC grid.
    assert len(autumn) == 96 and autumn[8] == 10.0 and autumn[12] == 16.0
    with pytest.raises(ValueError):
        local_curve(DAY, [1.0] * 92)


def test_inputs_use_only_days_available_at_the_cutoff(tmp_path):
    seed_days(tmp_path, DAY - timedelta(days=30), DAY)
    archive = load_archive(tmp_path)
    inputs = LearInputs(archive, covariates(DAY - timedelta(days=30), DAY))
    continuous, indicators = inputs(DAY)
    assert len(continuous) == 4 * 96 + 3 * 96 + 3 * 24 and len(indicators) == 8
    newest = last_label_day(DAY)
    assert list(continuous[:96]) == inputs.prices(newest)
    # Changing the delivery day's own prices cannot change its inputs.
    archive[DAY] = (archive[DAY][0], [])
    assert list(LearInputs(archive, covariates(DAY - timedelta(days=30), DAY))(DAY)[0]) == list(
        continuous
    )
    assert LearInputs(archive, {"load": {}, "weather": {}})(DAY) is None


def test_fit_and_predict_return_one_price_per_slot(tmp_path):
    seed_days(tmp_path, DAY - timedelta(days=40), DAY)
    archive = load_archive(tmp_path)
    inputs = LearInputs(archive, covariates(DAY - timedelta(days=40), DAY))
    fitted = fit_window(inputs, DAY - timedelta(days=25), DAY - timedelta(days=1))
    assert fitted["days"] == 25 and len(fitted["models"]) == 96
    curve = predict_window(fitted, inputs(DAY))
    assert curve.shape == (96,) and np.isfinite(curve).all()
    assert fit_window(inputs, DAY - timedelta(days=3), DAY - timedelta(days=1)) is None
