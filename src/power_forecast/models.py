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


def build(kind: str, params: dict, seed: int):
    if kind == "ridge":
        encode = ColumnTransformer(
            [
                ("calendar", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_COLUMNS),
            ],
            # Every other column, core or covariate, is scaled on training rows.
            remainder=StandardScaler(),
        )
        return make_pipeline(encode, Ridge(**params))
    if kind == "lightgbm":
        return LGBMRegressor(
            **params, random_state=seed, deterministic=True, force_row_wise=True, verbose=-1
        )
    raise ValueError(f"Unknown model {kind!r}.")


class FeatureCache:
    """Feature rows per day; they depend only on the day's own cutoff, not the origin."""

    def __init__(self, archive: dict, covariates: dict | None = None):
        self.archive = archive
        self.lookup = slot_lookup(archive.values())
        # Price covariates need a slot lookup; weather and load are used as given.
        self.covariates = {
            name: values if name in ("weather", "load") else (values, slot_lookup(values.values()))
            for name, values in (covariates or {}).items()
        }
        self.rows = {}

    def __call__(self, day: date):
        if day not in self.rows:
            self.rows[day] = day_features(self.archive, self.lookup, day, self.covariates)
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


def _quantile_models(features, targets, config: dict) -> list:
    return [
        build(
            "lightgbm",
            config["models"]["lightgbm"] | {"objective": "quantile", "alpha": q},
            config["seed"],
        ).fit(features, targets)
        for q in config["quantiles"]
    ]


def _predict_quantiles(models: list, features) -> np.ndarray:
    return np.column_stack([model.predict(features) for model in models])


HOUR = FEATURES.index("hour")
# A calibration group thinner than this uses the all-hours value instead.
MIN_GROUP = 20


def _grouped(cal_keys, values, statistic, keys):
    """`statistic` of `values` per calibration group, looked up for each of `keys`."""
    overall = statistic(values)
    table = {
        key: statistic(values[cal_keys == key])
        for key in np.unique(cal_keys)
        if np.count_nonzero(cal_keys == key) >= MIN_GROUP
    }
    return np.array([table.get(key, overall) for key in keys])


def probabilistic_forecaster(kind: str, cache: FeatureCache, train: tuple, config: dict):
    """Fit on one training window; return a function from feature rows to P10/P50/P90.

    The last `calibration_days` of the window are held back to measure errors the
    model makes on days it has not seen; the final models are refitted on the whole
    window. A `_by_hour` kind measures those errors separately per local hour.
    The returned function gives sorted quantiles, whether the raw quantiles crossed,
    and the conformal offset applied to each row.
    """
    base, by_hour = kind.removesuffix("_by_hour"), kind.endswith("_by_hour")
    first, last = train
    calibration_first = last - timedelta(days=config["calibration_days"] - 1)
    fit_x, fit_y = cache.matrix(first, calibration_first - timedelta(days=1))
    cal_x, cal_y = cache.matrix(calibration_first, last)
    all_x, all_y = cache.matrix(first, last)
    low, _, high = config["quantiles"]

    def groups(rows):
        return rows[:, HOUR] if by_hour else np.zeros(len(rows))

    if base == "ridge_interval":
        held_back = build("ridge", config["models"]["ridge"], config["seed"]).fit(fit_x, fit_y)
        residuals = cal_y - held_back.predict(cal_x)
        final = build("ridge", config["models"]["ridge"], config["seed"]).fit(all_x, all_y)

        def predict(rows):
            point = final.predict(rows)
            offsets = _grouped(
                groups(cal_x), residuals, lambda v: np.quantile(v, [low, high]), groups(rows)
            )
            # If a group's errors are all one-signed, clamp so the point stays the median.
            quantiles = np.column_stack(
                [
                    point + np.minimum(offsets[:, 0], 0),
                    point,
                    point + np.maximum(offsets[:, 1], 0),
                ]
            )
            return quantiles, np.zeros(len(point), dtype=bool), np.zeros(len(point))

        return predict
    if base == "lightgbm_cqr":
        held_back = np.sort(_predict_quantiles(_quantile_models(fit_x, fit_y, config), cal_x))
        scores = np.maximum(held_back[:, 0] - cal_y, cal_y - held_back[:, 2])

        def conformal(values):
            # Finite-sample conformal quantile for nominal (high - low) coverage. It may
            # be negative, narrowing an interval that over-covered on held-back days.
            level = min(1.0, (high - low) * (1 + 1 / len(values)))
            return float(np.quantile(values, level, method="higher"))

        final = _quantile_models(all_x, all_y, config)

        def predict(rows):
            raw = _predict_quantiles(final, rows)
            crossed = (raw[:, 0] > raw[:, 1]) | (raw[:, 1] > raw[:, 2])
            widen = _grouped(groups(cal_x), scores, conformal, groups(rows))
            ordered = np.sort(raw, axis=1)
            ordered[:, 0] -= widen
            ordered[:, 2] += widen
            return ordered, crossed, widen

        return predict
    raise ValueError(f"Unknown probabilistic model {kind!r}.")


def backtest_probabilistic(
    cache: FeatureCache, splits: dict, era: str, fold: str, kind: str, config: dict
) -> list[dict]:
    """Like backtest_model, with P10/P50/P90 per interval; `error` is P50 minus actual."""
    results = []
    window = config["windows"][era]
    for origin in rolling_origins(splits, era, fold, window):
        train = tuple(date.fromisoformat(d) for d in origin["train"])
        predict = probabilistic_forecaster(kind, cache, train, config)
        day, last = (date.fromisoformat(d) for d in origin["evaluate"])
        while day <= last:
            rows = cache(day)
            if rows is None:
                results.append({"day": day, "intervals": None})
                day += timedelta(days=1)
                continue
            quantiles, crossed, offsets = predict(np.array(rows, dtype=float))
            results.append(
                {
                    "day": day,
                    "intervals": [
                        {
                            "local": actual.start.astimezone(VIENNA),
                            "actual": actual.eur_mwh,
                            "error": float(q[1]) - actual.eur_mwh,
                            "fallback": False,
                            "quantiles": [float(v) for v in q],
                            "crossed": bool(c),
                            "conformal_offset": float(offset),
                        }
                        for q, c, offset, actual in zip(
                            quantiles, crossed, offsets, cache.archive[day][1], strict=True
                        )
                    ],
                }
            )
            day += timedelta(days=1)
    return results
