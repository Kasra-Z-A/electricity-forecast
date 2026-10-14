"""Backtest the seasonal baselines on the frozen validation folds; selects no model.

The holdout folds are not evaluated: they are consulted once, for release
assessment. Run from the repository root after experiments/freeze_splits.py:
    uv run python experiments/backtest_baselines.py
"""

import json
from datetime import date, timedelta
from pathlib import Path

from power_forecast.backtest import backtest, load_archive, trailing_high_price
from power_forecast.history import atomic_json
from power_forecast.metrics import paired_comparison, summarize
from power_forecast.splits import load_splits

STRATEGIES = ["weekly", "prior_day", "weekday_median"]
REFERENCE = "weekly"


def days_between(first: str, last: str) -> list[date]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def rounded(value):
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, dict):
        return {str(k): rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [rounded(v) for v in value]
    return value


def evaluate(archive: dict, days: list[date]) -> dict:
    results = backtest(archive, days, STRATEGIES)
    high_price = {day: trailing_high_price(archive, day) for day in days}
    return {
        "first": str(days[0]),
        "last": str(days[-1]),
        "models": {name: summarize(results[name], high_price) for name in STRATEGIES},
        f"versus_{REFERENCE}": {
            name: paired_comparison(results[name], results[REFERENCE])
            for name in STRATEGIES
            if name != REFERENCE
        },
    }


def main() -> None:
    config = load_splits(Path("configs/splits.json"))
    frozen = json.loads(Path("reports/splits.json").read_text())
    if frozen["config_sha256"] != config["sha256"]:
        raise SystemExit("configs/splits.json changed since it was frozen; refreeze first.")
    archive = load_archive(Path("data"))
    eras = {}
    for name, era in config["eras"].items():
        eras[name] = {
            "validation": evaluate(archive, days_between(*era["validation"])),
            "slices": {
                slice_name: evaluate(archive, days_between(*window))
                for slice_name, window in era.get("slices", {}).items()
            },
        }
        weekly = eras[name]["validation"]["models"][REFERENCE]
        print(f"{name}: weekly validation MAE {weekly['mae']:.2f} over {weekly['days']} days")
        for model, comparison in eras[name]["validation"][f"versus_{REFERENCE}"].items():
            low, high = comparison["difference_95"]
            print(
                f"  {model}: skill {comparison['mae_skill']:+.3f}, "
                f"daily MAE difference 95% [{low:+.2f}, {high:+.2f}]"
            )
    report = {
        "evaluation": "rolling daily replay of deterministic baselines on frozen validation folds",
        "splits_sha256": config["sha256"],
        "reference": REFERENCE,
        "metric_units": "EUR/MWh; MAE and RMSE are interval-weighted unless named daily_mean",
        "high_price_slice": "actual at or above the P95 of the 28 days of results "
        "available at that day's cutoff",
        "uncertainty": "week-block bootstrap of paired daily MAE differences, 2000 replicates, seed 0",
        "eras": rounded(eras),
        "caveats": [
            "Inputs are the latest archived versions, all retrieved on or after 2026-09-17, "
            "not the vintages available at each past cutoff.",
            "Baselines are not fitted, so weekly refits do not apply; each day uses only "
            "days whose result was available at its own cutoff.",
            "Holdout folds were not evaluated.",
        ],
    }
    atomic_json(Path("reports/backtest_baselines.json"), report)


if __name__ == "__main__":
    main()
