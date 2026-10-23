"""M8 comparison: LEAR, LightGBM with weather and load, Chronos-2 and their ensemble.

Every model and reference is scored on the same days, refitted weekly at the
cutoff, with settings fixed in configs/comparison.json before any of them was
scored. The selection window picks one model by the rule in that file; the
confirmation window is scored once afterwards. Run from the repository root:
    uv run power-forecast backfill-weather 2024-03-15 2026-10-21
    uv run power-forecast backfill-load 2018-10-01 2026-10-22
    uv run --group research --group foundation python experiments/compare_models.py validation
    uv run --group research --group foundation python experiments/compare_models.py holdout
"""

import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path

from backtest_baselines import days_between, rounded

from power_forecast import chronos
from power_forecast.backtest import backtest, load_archive, trailing_high_price
from power_forecast.history import atomic_json
from power_forecast.ingestion.load import load_load
from power_forecast.ingestion.weather import load_weather
from power_forecast.lear import LearInputs, backtest_lear
from power_forecast.metrics import paired_comparison, probabilistic_summary, summarize
from power_forecast.models import FeatureCache, backtest_model
from power_forecast.splits import load_splits

CONFIG = Path("configs/comparison.json")
REPORTS = {
    "validation": Path("reports/comparison_selection.json"),
    "holdout": Path("reports/comparison_confirmation.json"),
}
CANDIDATES = ["lear", "lightgbm", "chronos2", "ensemble"]
REFERENCES = ["prior_day", "weekly", "ridge_core_180"]


def ensemble(members: list[list[dict]]) -> list[dict]:
    """Equal-weight mean of member point forecasts; a day needs every member."""
    output = []
    for days in zip(*members, strict=True):
        if any(day["intervals"] is None for day in days):
            output.append({"day": days[0]["day"], "intervals": None})
            continue
        intervals = []
        for items in zip(*(day["intervals"] for day in days), strict=True):
            if len({item["local"] for item in items}) != 1:
                raise ValueError(f"Members disagree on the grid of {days[0]['day']}.")
            intervals.append(
                {
                    "local": items[0]["local"],
                    "actual": items[0]["actual"],
                    "error": sum(item["error"] for item in items) / len(items),
                    "fallback": False,
                }
            )
        output.append({"day": days[0]["day"], "intervals": intervals})
    return output


def check_weights(data_dir: Path) -> None:
    path = chronos.model_dir(data_dir) / "model.safetensors"
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != chronos.WEIGHTS_SHA256:
        raise SystemExit("Chronos-2 weights do not match the pinned checksum.")


def main(fold: str) -> None:
    if fold not in REPORTS:
        raise SystemExit("Fold must be 'validation' (selection) or 'holdout' (confirmation).")
    splits = load_splits(CONFIG)
    selected = None
    if fold == "holdout":
        if REPORTS["holdout"].exists():
            raise SystemExit("The confirmation window has already been scored.")
        selection = json.loads(REPORTS["validation"].read_text())
        if selection["config_sha256"] != splits["sha256"]:
            raise SystemExit("configs/comparison.json changed after selection.")
        selected = selection["selected"]
    models_bytes = Path("configs/backtest_models.json").read_bytes()
    models_config = json.loads(models_bytes)
    data_dir = Path("data")
    archive = load_archive(data_dir)
    covariates = {"load": load_load(data_dir), "weather": load_weather(data_dir)}
    spec = splits["eras"]["native"]
    settings = splits["models"]
    days = days_between(*spec[fold])

    results = backtest(archive, days, ["prior_day", "weekly"])
    results["ridge_core_180"] = backtest_model(
        FeatureCache(archive), splits, "native", fold, 180, "ridge", models_config
    )
    print("references done", flush=True)
    results["lear"] = backtest_lear(
        LearInputs(archive, covariates), splits, "native", fold, settings["lear"]["windows"]
    )
    print("lear done", flush=True)
    lightgbm = settings["lightgbm"]
    results["lightgbm"] = backtest_model(
        FeatureCache(archive, {name: covariates[name] for name in lightgbm["covariates"]}),
        splits,
        "native",
        fold,
        lightgbm["window"],
        "lightgbm",
        models_config,
    )
    print("lightgbm done", flush=True)
    check_weights(data_dir)
    forecasts = chronos.forecast_isolated(data_dir, days)
    results["chronos2"] = chronos.results(archive, forecasts, days)
    print("chronos2 done", flush=True)
    results["ensemble"] = ensemble([results[name] for name in settings["ensemble"]["members"]])

    covered = {
        name: {day["day"] for day in result if day["intervals"]} for name, result in results.items()
    }
    common = set(days).intersection(*covered.values())
    excluded = {
        str(day): sorted(name for name in results if day not in covered[name])
        for day in days
        if day not in common
    }
    scored = {
        name: [day for day in result if day["day"] in common] for name, result in results.items()
    }
    high_price = {day: trailing_high_price(archive, day) for day in common}
    models = {name: summarize(result, high_price) for name, result in scored.items()}
    for name in CANDIDATES + REFERENCES:
        print(f"{name}: MAE {models[name]['mae']:.2f}, RMSE {models[name]['rmse']:.2f}")
    if fold == "validation":
        selected = min(CANDIDATES, key=lambda name: models[name]["mae"])
    print(f"selected: {selected}")

    report = {
        "evaluation": "M8 model comparison, weekly-refit rolling origins on common days",
        "fold": {"validation": "selection", "holdout": "confirmation"}[fold],
        "window": spec[fold],
        "config_sha256": splits["sha256"],
        "models_config_sha256": hashlib.sha256(models_bytes).hexdigest(),
        "lock_sha256": hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest(),
        "packages": {
            name: version(name)
            for name in ("numpy", "scikit-learn", "lightgbm", "chronos-forecasting", "torch")
        },
        "chronos2": {"model": chronos.MODEL, "revision": chronos.REVISION},
        "selection_rule": splits["selection"],
        "selected": selected,
        "common_days": len(common),
        "excluded_days": excluded,
        "metric_units": "EUR/MWh; MAE and RMSE are interval-weighted unless named daily_mean",
        "uncertainty": "week-block bootstrap of paired daily MAE differences, 2000 replicates, seed 0",
        "models": models,
        "chronos2_probabilistic": probabilistic_summary(scored["chronos2"]),
        **{
            f"versus_{reference}": {
                name: paired_comparison(scored[name], scored[reference]) for name in CANDIDATES
            }
            for reference in REFERENCES
        },
        "caveats": [
            "Weather and load forecasts are archived versions retrieved in October 2026, not "
            "the vintages available at each past cutoff; their published schedules (ECMWF 00 "
            "UTC run of D-1, load forecast due 10:00 on D-1) are the availability evidence.",
            "Models train on native quarter-hour data only, so training windows are at most "
            "about six months at the start of the selection window.",
            "The selection window includes the native holdout consulted once on 2026-10-17 for "
            "ridge-core-180-v1; no setting here was chosen on it.",
            "Chronos-2 is used zero-shot with pinned weights; it was released before these "
            "windows, so they cannot be part of its training data.",
            "No winter days are evaluated.",
        ],
    }
    atomic_json(REPORTS[fold], rounded(report))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "validation")
