"""Ridge regression for production, in numpy only, stored as plain numbers.

It reproduces the research pipeline in models.py (one-hot calendar columns, other
columns standardized on training rows, Ridge with an unpenalized intercept), so the
serving image needs no scikit-learn and a fitted model is JSON rather than a pickle.
tests/test_ridge.py checks the predictions against scikit-learn.
"""

import numpy as np

from power_forecast.features import CATEGORICAL, FEATURES


def _design(params: dict, rows) -> np.ndarray:
    x = np.asarray(rows, dtype=float)
    categorical = [FEATURES.index(name) for name in CATEGORICAL]
    other = [i for i in range(x.shape[1]) if i not in categorical]
    # Unseen categories encode as all zeros, as with handle_unknown="ignore".
    encoded = [
        (x[:, column, None] == np.asarray(values)[None, :]).astype(float)
        for column, values in zip(categorical, params["categories"], strict=True)
    ]
    scaled = (x[:, other] - np.asarray(params["means"])) / np.asarray(params["scales"])
    return np.hstack([*encoded, scaled])


def fit(rows, targets, alpha: float) -> dict:
    x = np.asarray(rows, dtype=float)
    y = np.asarray(targets, dtype=float)
    categorical = [FEATURES.index(name) for name in CATEGORICAL]
    other = [i for i in range(x.shape[1]) if i not in categorical]
    means = x[:, other].mean(axis=0)
    scales = x[:, other].std(axis=0)
    scales[scales == 0] = 1.0  # constant columns, as StandardScaler handles them
    params = {
        "categories": [sorted(set(x[:, column].tolist())) for column in categorical],
        "means": means.tolist(),
        "scales": scales.tolist(),
    }
    design = _design(params, x)
    centre, target_mean = design.mean(axis=0), y.mean()
    centred = design - centre
    gram = centred.T @ centred + alpha * np.eye(design.shape[1])
    coef = np.linalg.solve(gram, centred.T @ (y - target_mean))
    return params | {
        "alpha": alpha,
        "coef": coef.tolist(),
        "intercept": float(target_mean - centre @ coef),
    }


def predict(params: dict, rows) -> list[float]:
    return (_design(params, rows) @ np.asarray(params["coef"]) + params["intercept"]).tolist()
