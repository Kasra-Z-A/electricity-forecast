"""Backtest the learned models against the seasonal baselines on validation folds.

Holdout folds are not evaluated. Run from the repository root with the research
group after experiments/freeze_splits.py:
    uv run --group research python experiments/backtest_models.py
"""

import hashlib
import json
from importlib.metadata import version
from pathlib import Path

from backtest_baselines import days_between, rounded

from power_forecast.backtest import backtest, load_archive, trailing_high_price
from power_forecast.features import FEATURES
from power_forecast.history import atomic_json
from power_forecast.metrics import paired_comparison, summarize
from power_forecast.models import FeatureCache, backtest_model
from power_forecast.splits import load_splits

BASELINES = ["weekly", "prior_day"]
MODELS = ["ridge", "lightgbm"]


def main() -> None:
    splits = load_splits(Path("configs/splits.json"))
    frozen = json.loads(Path("reports/splits.json").read_text())
    if frozen["config_sha256"] != splits["sha256"]:
        raise SystemExit("configs/splits.json changed since it was frozen; refreeze first.")
    config_bytes = Path("configs/backtest_models.json").read_bytes()
    config = json.loads(config_bytes)
    archive = load_archive(Path("data"))
    cache = FeatureCache(archive)
    eras = {}
    for era, spec in splits["eras"].items():
        days = days_between(*spec["validation"])
        high_price = {day: trailing_high_price(archive, day) for day in days}
        results = backtest(archive, days, BASELINES)
        for kind in MODELS:
            for window in splits["training_windows"]:
                name = f"{kind}_{window}"
                results[name] = backtest_model(
                    cache, splits, era, "validation", window, kind, config
                )
                print(f"{era} {name}: {summarize(results[name], high_price)['mae']:.2f}")
        eras[era] = {
            "first": str(days[0]),
            "last": str(days[-1]),
            "models": {name: summarize(days_, high_price) for name, days_ in results.items()},
            **{
                f"versus_{reference}": {
                    name: paired_comparison(results[name], results[reference])
                    for name in results
                    if name not in BASELINES
                }
                for reference in BASELINES
            },
        }
        print(
            f"{era} weekly {eras[era]['models']['weekly']['mae']:.2f}, "
            f"prior_day {eras[era]['models']['prior_day']['mae']:.2f}"
        )
    report = {
        "evaluation": "weekly-refit rolling-origin backtest on frozen validation folds",
        "splits_sha256": splits["sha256"],
        "models_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "lock_sha256": hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest(),
        "packages": {name: version(name) for name in ("numpy", "scikit-learn", "lightgbm")},
        "features": list(FEATURES),
        "metric_units": "EUR/MWh; MAE and RMSE are interval-weighted unless named daily_mean",
        "uncertainty": "week-block bootstrap of paired daily MAE differences, 2000 replicates, seed 0",
        "eras": rounded(eras),
        "caveats": [
            "Inputs are the latest archived versions, all retrieved on or after 2026-09-17, "
            "not the vintages available at each past cutoff.",
            "Hyperparameters are fixed in configs/backtest_models.json and were not tuned; "
            "the training window is the only choice made on validation.",
            "Native history is short: validation covers 91 days in 14 week blocks.",
            "Holdout folds were not evaluated.",
        ],
    }
    atomic_json(Path("reports/backtest_models.json"), report)


if __name__ == "__main__":
    main()
