"""Backtest P10/P50/P90 forecasts and their calibration on the validation folds.

Holdout folds are not evaluated. Run from the repository root with the research
group after experiments/freeze_splits.py:
    uv run --group research python experiments/backtest_quantiles.py
"""

import hashlib
import json
from importlib.metadata import version
from pathlib import Path

from backtest_baselines import days_between, rounded

from power_forecast.backtest import backtest, load_archive, trailing_high_price
from power_forecast.history import atomic_json
from power_forecast.metrics import paired_comparison, probabilistic_summary, summarize
from power_forecast.models import FeatureCache, backtest_probabilistic
from power_forecast.splits import load_splits

KINDS = ["ridge_interval", "lightgbm_cqr"]
# Added on 2026-10-15 after the first run showed midday under-coverage; not preregistered.
LATER_KINDS = ["ridge_interval_by_hour", "lightgbm_cqr_by_hour"]


def raw_view(results: list[dict]) -> list[dict]:
    """The uncalibrated, ordered LightGBM quantiles behind a CQR result."""
    return [
        day
        | {
            "intervals": [
                item
                | {
                    "quantiles": [
                        item["quantiles"][0] + item["conformal_offset"],
                        item["quantiles"][1],
                        item["quantiles"][2] - item["conformal_offset"],
                    ]
                }
                for item in day["intervals"]
            ]
        }
        if day["intervals"]
        else day
        for day in results
    ]


def main() -> None:
    splits = load_splits(Path("configs/splits.json"))
    frozen = json.loads(Path("reports/splits.json").read_text())
    if frozen["config_sha256"] != splits["sha256"]:
        raise SystemExit("configs/splits.json changed since it was frozen; refreeze first.")
    config_bytes = Path("configs/backtest_quantiles.json").read_bytes()
    config = json.loads(config_bytes)
    archive = load_archive(Path("data"))
    cache = FeatureCache(archive)
    levels = tuple(config["quantiles"])
    eras = {}
    for era, spec in splits["eras"].items():
        days = days_between(*spec["validation"])
        high_price = {day: trailing_high_price(archive, day) for day in days}
        reference = backtest(archive, days, ["prior_day"])["prior_day"]
        results = {
            kind: backtest_probabilistic(cache, splits, era, "validation", kind, config)
            for kind in KINDS + LATER_KINDS
        }
        results["lightgbm_quantile_raw"] = raw_view(results["lightgbm_cqr"])
        eras[era] = {
            "first": str(days[0]),
            "last": str(days[-1]),
            "window": config["windows"][era],
            "probabilistic": {
                name: probabilistic_summary(value, levels) for name, value in results.items()
            },
            "point": {name: summarize(results[name], high_price) for name in KINDS},
            "point_versus_prior_day": {
                name: paired_comparison(results[name], reference) for name in KINDS
            },
        }
        for name, summary in eras[era]["probabilistic"].items():
            print(
                f"{era} {name}: coverage {summary['coverage']:.3f}, width "
                f"{summary['mean_width']:.1f}, worst hour {summary['worst_hour_coverage']:.3f}, "
                f"pinball {summary['pinball']}"
            )
    report = {
        "evaluation": "weekly-refit rolling-origin probabilistic backtest on validation folds",
        "splits_sha256": splits["sha256"],
        "quantiles_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "lock_sha256": hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest(),
        "packages": {name: version(name) for name in ("numpy", "scikit-learn", "lightgbm")},
        "models": {
            "ridge_interval": "ridge point forecast plus the P10/P90 of its errors on the "
            "last 28 training days, measured by a fit that had not seen them",
            "lightgbm_quantile_raw": "separate P10/P50/P90 LightGBM quantile models, sorted",
            "lightgbm_cqr": "the raw quantiles widened or narrowed symmetrically so the "
            "held-back 28 days reach 80% coverage (conformalized quantile regression)",
            "ridge_interval_by_hour": "ridge_interval with errors measured per local hour",
            "lightgbm_cqr_by_hour": "lightgbm_cqr with the conformal offset set per local hour",
        },
        "not_preregistered": {
            "kinds": LATER_KINDS,
            "reason": "added after the first run showed coverage near 70% at midday and over "
            "90% at night; the validation fold had been seen when they were designed",
        },
        "ordering_note": "ridge offsets are clamped so P10 <= point <= P90 when a group's "
        "errors are all one-signed",
        "ordering_rule": "quantiles are sorted per interval; raw_crossing_rate is measured first",
        "eras": rounded(eras),
        "caveats": [
            "Coverage inside the 70-90% screening band is a guardrail, not proof of "
            "calibration; see coverage_by_local_hour.",
            "Inputs are the latest archived versions, all retrieved on or after 2026-09-17, "
            "not the vintages available at each past cutoff.",
            "Holdout folds were not evaluated.",
        ],
    }
    atomic_json(Path("reports/backtest_quantiles.json"), report)


if __name__ == "__main__":
    main()
