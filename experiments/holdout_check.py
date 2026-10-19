"""Score the frozen M6 candidate once on the untouched native holdout.

The holdout is consulted a single time (docs/DESIGN.md, Evaluation and promotion): the script refuses to
run if its report already exists, and evaluates only configs/candidate.json against
the seasonal baselines, so no choice can be made on these days. Run from the
repository root with the research group:
    uv run --group research python experiments/holdout_check.py
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

REPORT = Path("reports/holdout_native.json")
BASELINES = ["weekly", "prior_day"]


def main() -> None:
    if REPORT.exists():
        raise SystemExit(f"{REPORT} exists: the holdout has been consulted and is spent.")
    splits = load_splits(Path("configs/splits.json"))
    frozen = json.loads(Path("reports/splits.json").read_text())
    if frozen["config_sha256"] != splits["sha256"]:
        raise SystemExit("configs/splits.json changed since it was frozen; refreeze first.")
    candidate_bytes = Path("configs/candidate.json").read_bytes()
    candidate = json.loads(candidate_bytes)
    config = {"seed": candidate["seed"], "models": {candidate["kind"]: candidate["params"]}}
    archive = load_archive(Path("data"))
    days = days_between(*splits["eras"]["native"]["holdout"])
    high_price = {day: trailing_high_price(archive, day) for day in days}
    results = backtest(archive, days, BASELINES)
    name = candidate["name"]
    results[name] = backtest_model(
        FeatureCache(archive),
        splits,
        "native",
        "holdout",
        candidate["training_window"],
        candidate["kind"],
        config,
    )
    models = {model: summarize(values, high_price) for model, values in results.items()}
    for model, summary in models.items():
        print(f"{model}: MAE {summary['mae']:.2f}")
    report = {
        "evaluation": "single consultation of the native holdout, weekly-refit rolling origins",
        "candidate": candidate,
        "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "splits_sha256": splits["sha256"],
        "lock_sha256": hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest(),
        "packages": {name: version(name) for name in ("numpy", "scikit-learn")},
        "features": list(FEATURES),
        "first": str(days[0]),
        "last": str(days[-1]),
        "models": rounded(models),
        **{
            f"versus_{reference}": rounded(paired_comparison(results[name], results[reference]))
            for reference in BASELINES
        },
        "caveats": [
            "Inputs are the latest archived versions, all retrieved on or after 2026-09-17, "
            "not the vintages available at each past cutoff.",
            "The holdout is now spent; later promotion evidence must come from forward days.",
        ],
    }
    atomic_json(REPORT, report)


if __name__ == "__main__":
    main()
