"""Ablation: does one covariate group improve the price-only models?

The group is added alone to the same model and training window on the same
validation days (docs/DESIGN.md, Models). Holdout folds are not evaluated. Run from the
repository root once the group's archive is complete:
    uv run power-forecast backfill-prices 2018-10-01 2026-10-15 --zone DE-LU --window-days 31
    uv run --group research python experiments/ablation.py de_lu
    uv run power-forecast backfill-weather 2025-10-01 2026-10-18
    uv run --group research python experiments/ablation.py weather
"""

import hashlib
import json
import sys
from pathlib import Path

from backtest_baselines import days_between, rounded

from power_forecast.backtest import load_archive, trailing_high_price
from power_forecast.coverage import coverage
from power_forecast.features import COVARIATES
from power_forecast.history import atomic_json
from power_forecast.ingestion.weather import MODEL, load_weather
from power_forecast.metrics import paired_comparison, summarize
from power_forecast.models import FeatureCache, backtest_model
from power_forecast.splits import load_splits

MODELS = ["ridge", "lightgbm"]
# The better window per era from reports/backtest_models.json, fixed before any ablation.
WINDOWS = {"hourly": "expanding", "native": 180}


def load_de_lu() -> tuple[dict, dict]:
    directory = Path("data/zones/DE-LU")
    report = coverage(directory, verify=True)
    if not report.get("complete"):
        raise SystemExit("DE-LU archive is incomplete; finish the backfill first.")
    summary = {key: report[key] for key in ("days", "span", "missing_days", "complete")}
    return load_archive(directory), summary


def load_weather_runs() -> tuple[dict, dict]:
    runs = load_weather(Path("data"))
    return runs, {"days": len(runs), "first": str(min(runs)), "last": str(max(runs))}


GROUPS = {
    "de_lu": {
        "eras": ["hourly", "native"],
        "load": load_de_lu,
        "eligibility": "DE-LU D-1 and earlier prices come from the same auction as the AT "
        "target and are available at the cutoff on the same evidence; see SOURCE_AUDIT.md",
        "caveats": [
            "Inputs are archived versions retrieved in September-October 2026, not past vintages."
        ],
    },
    "weather": {
        "eras": ["native"],
        "load": load_weather_runs,
        "eligibility": f"Open-Meteo {MODEL} 00 UTC single run of D-1, typically available "
        "4-6 hours later, before the cutoff; reconstructed, not a verified vintage; "
        "data CC BY 4.0 Open-Meteo.com",
        "caveats": [
            "Archived runs before 2026 may be IFS Cycle 49R1 hindcasts; IFS Cycle 50R1 "
            "from 2026-05-12 falls inside the validation fold.",
            "Weather exists only from 2024-03-14, so the hourly era is not ablated.",
            "Six city points are regional proxies, not a capacity-weighted grid.",
        ],
    },
}


def main() -> None:
    group = sys.argv[1] if len(sys.argv) > 1 else ""
    if group not in GROUPS:
        raise SystemExit(f"Usage: ablation.py {{{','.join(GROUPS)}}}")
    spec = GROUPS[group]
    splits = load_splits(Path("configs/splits.json"))
    config_bytes = Path("configs/backtest_models.json").read_bytes()
    config = json.loads(config_bytes)
    values, archive_summary = spec["load"]()
    archive = load_archive(Path("data"))
    core = FeatureCache(archive)
    added = FeatureCache(archive, {group: values})
    eras = {}
    for era in spec["eras"]:
        days = days_between(*splits["eras"][era]["validation"])
        high_price = {day: trailing_high_price(archive, day) for day in days}
        eras[era] = {"window": WINDOWS[era], "models": {}}
        for kind in MODELS:
            base = backtest_model(core, splits, era, "validation", WINDOWS[era], kind, config)
            more = backtest_model(added, splits, era, "validation", WINDOWS[era], kind, config)
            # Days without the group's inputs are excluded from both sides and reported.
            excluded = [str(day["day"]) for day in more if day["intervals"] is None]
            common = [day for day in base if str(day["day"]) not in excluded]
            comparison = paired_comparison(more, common)
            eras[era]["models"][kind] = {
                "excluded_days": excluded,
                "core": summarize(common, high_price),
                f"core_plus_{group}": summarize(
                    [day for day in more if day["intervals"]], high_price
                ),
                f"{group}_versus_core": comparison,
            }
            low, high = comparison["difference_95"]
            print(
                f"{era} {kind}: skill {comparison['mae_skill']:+.3f}, "
                f"daily MAE difference 95% [{low:+.2f}, {high:+.2f}]"
            )
    report = {
        "evaluation": "single-group ablation on frozen validation folds",
        "group": {group: list(COVARIATES[group])},
        "eligibility": spec["eligibility"],
        "splits_sha256": splits["sha256"],
        "models_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        f"{group}_archive": archive_summary,
        "eras": rounded(eras),
        "caveats": spec["caveats"]
        + [
            "Hyperparameters are the M3 preregistered values; the group is not tuned.",
            "Holdout folds were not evaluated.",
        ],
    }
    atomic_json(Path(f"reports/ablation_{group}.json"), report)


if __name__ == "__main__":
    main()
