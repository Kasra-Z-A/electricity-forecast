"""Ablation: does adding lagged DE-LU prices improve the price-only models?

One covariate group, added alone, to the same model and training window on the same
validation days (PLAN.md section 7). Holdout folds are not evaluated. Run from the
repository root after backfilling DE-LU into data/zones/DE-LU:
    uv run power-forecast backfill-prices 2018-10-01 2026-10-15 --zone DE-LU
    uv run --group research python experiments/ablation_de_lu.py
"""

import hashlib
import json
from pathlib import Path

from backtest_baselines import days_between, rounded

from power_forecast.backtest import load_archive, trailing_high_price
from power_forecast.coverage import coverage
from power_forecast.features import COVARIATES
from power_forecast.history import atomic_json
from power_forecast.metrics import paired_comparison, summarize
from power_forecast.models import FeatureCache, backtest_model
from power_forecast.splits import load_splits

MODELS = ["ridge", "lightgbm"]
# The better window per era from reports/backtest_models.json, fixed before this run.
WINDOWS = {"hourly": "expanding", "native": 180}


def main() -> None:
    splits = load_splits(Path("configs/splits.json"))
    config_bytes = Path("configs/backtest_models.json").read_bytes()
    config = json.loads(config_bytes)
    de_dir = Path("data/zones/DE-LU")
    de_coverage = coverage(de_dir, verify=True)
    if not de_coverage.get("complete"):
        raise SystemExit("DE-LU archive is incomplete; finish the backfill first.")
    archive = load_archive(Path("data"))
    core = FeatureCache(archive)
    with_de = FeatureCache(archive, {"de_lu": load_archive(de_dir)})
    eras = {}
    for era, spec in splits["eras"].items():
        days = days_between(*spec["validation"])
        high_price = {day: trailing_high_price(archive, day) for day in days}
        eras[era] = {"window": WINDOWS[era], "models": {}}
        for kind in MODELS:
            base = backtest_model(core, splits, era, "validation", WINDOWS[era], kind, config)
            added = backtest_model(with_de, splits, era, "validation", WINDOWS[era], kind, config)
            comparison = paired_comparison(added, base)
            eras[era]["models"][kind] = {
                "core": summarize(base, high_price),
                "core_plus_de_lu": summarize(added, high_price),
                "de_lu_versus_core": comparison,
            }
            low, high = comparison["difference_95"]
            print(
                f"{era} {kind}: skill {comparison['mae_skill']:+.3f}, "
                f"daily MAE difference 95% [{low:+.2f}, {high:+.2f}]"
            )
    report = {
        "evaluation": "single-group ablation on frozen validation folds",
        "group": {"de_lu": list(COVARIATES["de_lu"])},
        "eligibility": "DE-LU D-1 and earlier prices come from the same auction as the AT "
        "target and are available at the cutoff on the same evidence; see SOURCE_AUDIT.md",
        "splits_sha256": splits["sha256"],
        "models_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "de_lu_coverage": {
            key: de_coverage[key] for key in ("days", "span", "missing_days", "complete")
        },
        "eras": rounded(eras),
        "caveats": [
            "Inputs are archived versions retrieved in September-October 2026, not past vintages.",
            "Hyperparameters are the M3 preregistered values; the group is not tuned.",
            "Holdout folds were not evaluated.",
        ],
    }
    atomic_json(Path("reports/ablation_de_lu.json"), report)


if __name__ == "__main__":
    main()
