"""Learned models for research backtests; needs the `research` dependency group.

Production never imports this module, so its packages stay out of the image.
Every transformation (one-hot encoding, scaling) is fitted on training rows only.
"""

from datetime import date, timedelta

import numpy as np
from lightgbm import LGBMRegressor
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from power_forecast.baseline import slot_lookup
from power_forecast.features import CATEGORICAL, FEATURES, day_features
from power_forecast.splits import rolling_origins
from power_forecast.timegrid import VIENNA

CATEGORICAL_COLUMNS = [FEATURES.index(name) for name in CATEGORICAL]
NUMERIC_COLUMNS = [i for i in range(len(FEATURES)) if i not in CATEGORICAL_COLUMNS]


def build(kind: str, params: dict, seed: int):
    if kind == "ridge":
        encode = ColumnTransformer(
            [
                ("calendar", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_COLUMNS),
                ("numeric", StandardScaler(), NUMERIC_COLUMNS),
            ]
        )
        return make_pipeline(encode, Ridge(**params))
    if kind == "lightgbm":
        return LGBMRegressor(
            **params, random_state=seed, deterministic=True, force_row_wise=True, verbose=-1
        )
    raise ValueError(f"Unknown model {kind!r}.")


class FeatureCache:
    """Feature rows per day; they depend only on the day's own cutoff, not the origin."""

    def __init__(self, archive: dict):
        self.archive = archive
        self.lookup = slot_lookup(archive.values())
        self.rows = {}

    def __call__(self, day: date):
        if day not in self.rows:
            self.rows[day] = day_features(self.archive, self.lookup, day)
        return self.rows[day]

    def matrix(self, first: date, last: date) -> tuple[np.ndarray, np.ndarray]:
        features, targets = [], []
        day = first
        while day <= last:
            rows = self(day)
            if rows is not None and day in self.archive:
                features += rows
                targets += [row.eur_mwh for row in self.archive[day][1]]
            day += timedelta(days=1)
        return np.array(features, dtype=float), np.array(targets, dtype=float)


def backtest_model(
    cache: FeatureCache, splits: dict, era: str, fold: str, window, kind: str, config: dict
) -> list[dict]:
    """Refit at every origin and forecast its days, in the backtest.py result format."""
    results = []
    for origin in rolling_origins(splits, era, fold, window):
        train_first, train_last = (date.fromisoformat(d) for d in origin["train"])
        features, targets = cache.matrix(train_first, train_last)
        model = build(kind, config["models"][kind], config["seed"]).fit(features, targets)
        day, last = (date.fromisoformat(d) for d in origin["evaluate"])
        while day <= last:
            rows = cache(day)
            if rows is None:
                results.append({"day": day, "intervals": None})
            else:
                points = model.predict(np.array(rows, dtype=float))
                actuals = cache.archive[day][1]
                results.append(
                    {
                        "day": day,
                        "intervals": [
                            {
                                "local": actual.start.astimezone(VIENNA),
                                "actual": actual.eur_mwh,
                                "error": float(point) - actual.eur_mwh,
                                "fallback": False,
                            }
                            for point, actual in zip(points, actuals, strict=True)
                        ],
                    }
                )
            day += timedelta(days=1)
    return results
