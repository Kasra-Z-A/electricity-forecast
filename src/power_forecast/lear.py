"""LEAR (Lago et al. 2021): one Lasso per local delivery slot; needs the research group.

Every slot model sees the same day-level inputs: the whole price curves of D-1, D-2,
D-3 and D-7, the load forecast curves for D, D-1 and D-7, the hourly weather run for
D, and weekday and holiday indicators. Prices and loads are scaled by the median and
MAD of the training window and passed through asinh, which damps price spikes;
forecasts are mapped back the same way. Forecasts from several training windows are
averaged (Hubicka, Marcjasz and Weron 2019), so no single window has to be chosen.

The Lasso penalty is chosen per slot by contiguous five-fold cross-validation inside
the training window. The original uses AIC, which needs more training days than
inputs; native windows are shorter than that.
"""

import warnings
from datetime import date, timedelta

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LassoLarsCV
from sklearn.model_selection import KFold

from power_forecast.features import WEATHER_VARIABLES, _weather_hours, austrian_holidays
from power_forecast.history import resolution
from power_forecast.splits import last_label_day, rolling_origins
from power_forecast.timegrid import VIENNA, delivery_grid

SLOTS = 96
PRICE_LAGS = (1, 2, 3, 7)
LOAD_DAYS = (0, 1, 7)


def slot(moment) -> int:
    """Local quarter-hour position 0..95; both DST folds share their clock slot."""
    local = moment.astimezone(VIENNA)
    return local.hour * 4 + local.minute // 15


def local_curve(day: date, values: list[float], minutes: int = 15) -> list[float]:
    """Values on `day`'s UTC grid as 96 local clock slots.

    The repeated autumn hour is averaged; the missing spring hour repeats the hour
    before it. Neither rule uses any value from another day.
    """
    grid = delivery_grid(day, minutes)
    if len(values) != len(grid):
        raise ValueError(f"{day}: {len(values)} values for {len(grid)} intervals.")
    width = minutes // 15
    buckets = [[] for _ in range(SLOTS)]
    for moment, value in zip(grid, values, strict=True):
        first = slot(moment)
        for offset in range(width):
            buckets[first + offset].append(value)
    curve = []
    for bucket in buckets:
        curve.append(sum(bucket) / len(bucket) if bucket else curve[-4])
    return curve


class LearInputs:
    """Day-level input vectors and local target curves, cached per day."""

    def __init__(self, archive: dict, covariates: dict):
        self.archive, self.load, self.weather = archive, covariates["load"], covariates["weather"]
        self.cache = {}

    def prices(self, day: date) -> list[float] | None:
        if day not in self.archive or resolution(day) != 15:
            return None
        return local_curve(day, [row.eur_mwh for row in self.archive[day][1]])

    def _load(self, day: date) -> list[float] | None:
        return local_curve(day, self.load[day]["mw"]) if day in self.load else None

    def _weather(self, day: date) -> list[float] | None:
        if day not in self.weather:
            return None
        hours = _weather_hours(self.weather[day], day)
        local = {}
        for moment in delivery_grid(day, 60):
            if moment not in hours:
                return None
            local.setdefault(moment.astimezone(VIENNA).hour, []).append(hours[moment])
        if len(local) != 23 and len(local) != 24:
            return None
        values = []
        for index in range(len(WEATHER_VARIABLES)):
            by_hour = [
                np.mean([v[index] for v in local[h]]) if h in local else None for h in range(24)
            ]
            # The missing spring hour repeats the hour before it, as in local_curve.
            values += [v if v is not None else by_hour[h - 1] for h, v in enumerate(by_hour)]
        return values

    def __call__(self, day: date) -> tuple[np.ndarray, np.ndarray] | None:
        """(prices and loads to scale, indicators), or None if any input is missing."""
        if day in self.cache:
            return self.cache[day]
        newest = last_label_day(day)
        prices = [self.prices(newest - timedelta(days=lag - 1)) for lag in PRICE_LAGS]
        loads = [self._load(day - timedelta(days=lag)) for lag in LOAD_DAYS]
        weather = self._weather(day)
        result = None
        if None not in prices and None not in loads and weather is not None:
            weekday = [float(day.weekday() == n) for n in range(7)]
            indicators = weekday + [float(day in austrian_holidays(day.year))]
            continuous = [v for curve in prices + loads for v in curve] + weather
            result = (np.array(continuous), np.array(indicators))
        self.cache[day] = result
        return result


def _scaler(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    median = np.median(values, axis=0)
    mad = 1.4826 * np.median(np.abs(values - median), axis=0)
    return median, np.where(mad > 0, mad, 1.0)


def fit_window(inputs: LearInputs, first: date, last: date) -> dict | None:
    """96 slot models trained on days FIRST..LAST, with the scalers they were fitted with."""
    continuous, indicators, targets = [], [], []
    day = first
    while day <= last:
        row, curve = inputs(day), inputs.prices(day)
        if row is not None and curve is not None:
            continuous.append(row[0])
            indicators.append(row[1])
            targets.append(curve)
        day += timedelta(days=1)
    if len(targets) < 10:
        return None
    x_center, x_scale = _scaler(np.array(continuous))
    y_center, y_scale = _scaler(np.array(targets))
    x = np.hstack([np.arcsinh((np.array(continuous) - x_center) / x_scale), indicators])
    y = np.arcsinh((np.array(targets) - y_center) / y_scale)
    folds = KFold(5, shuffle=False)
    models = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        for column in range(SLOTS):
            models.append(LassoLarsCV(cv=folds, max_iter=500).fit(x, y[:, column]))
    return {
        "x_center": x_center,
        "x_scale": x_scale,
        "y_center": y_center,
        "y_scale": y_scale,
        "models": models,
        "days": len(targets),
    }


def predict_window(fitted: dict, row: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    """96 local-slot prices for one day."""
    x = np.hstack([np.arcsinh((row[0] - fitted["x_center"]) / fitted["x_scale"]), row[1]])
    scaled = np.array([model.predict(x[None, :])[0] for model in fitted["models"]])
    return np.sinh(scaled) * fitted["y_scale"] + fitted["y_center"]


def backtest_lear(
    inputs: LearInputs, splits: dict, era: str, fold: str, windows: list
) -> list[dict]:
    """Weekly refits per window, forecasts averaged across windows, in backtest.py format."""
    per_window = [rolling_origins(splits, era, fold, window) for window in windows]
    results = []
    for origins in zip(*per_window, strict=True):
        fitted = [
            fit_window(inputs, *(date.fromisoformat(d) for d in origin["train"]))
            for origin in origins
        ]
        fitted = [f for f in fitted if f is not None]
        day, last = (date.fromisoformat(d) for d in origins[0]["evaluate"])
        while day <= last:
            row = inputs(day)
            if row is None or not fitted or day not in inputs.archive:
                results.append({"day": day, "intervals": None})
            else:
                curve = np.mean([predict_window(f, row) for f in fitted], axis=0)
                results.append(
                    {
                        "day": day,
                        "intervals": [
                            {
                                "local": actual.start.astimezone(VIENNA),
                                "actual": actual.eur_mwh,
                                "error": float(curve[slot(actual.start)]) - actual.eur_mwh,
                                "fallback": False,
                            }
                            for actual in inputs.archive[day][1]
                        ],
                    }
                )
            day += timedelta(days=1)
    return results
