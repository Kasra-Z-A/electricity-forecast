"""Freeze evaluation windows against the audited archive; this selects no model.

Run from the repository root after experiments/coverage_report.py:
    uv run python experiments/freeze_splits.py
"""

import json
from datetime import date
from pathlib import Path

from power_forecast.history import atomic_json
from power_forecast.splits import check_coverage, load_splits, rolling_origins


def main() -> None:
    config = load_splits(Path("configs/splits.json"))
    coverage = json.loads(Path("reports/coverage.json").read_text())
    problems = check_coverage(config, coverage)
    if problems:
        raise SystemExit("Cannot freeze splits: " + "; ".join(problems))
    eras = {}
    for name in config["eras"]:
        eras[name] = {}
        for fold in ("validation", "holdout"):
            runs = {
                str(window): rolling_origins(config, name, fold, window)
                for window in config["training_windows"]
            }
            eras[name][fold] = {
                "origins": len(next(iter(runs.values()))),
                "first_origin_train_days": {
                    window: (
                        date.fromisoformat(origins[0]["train"][1])
                        - date.fromisoformat(origins[0]["train"][0])
                    ).days
                    + 1
                    for window, origins in runs.items()
                },
            }
    report = {
        "config": "configs/splits.json",
        "config_sha256": config["sha256"],
        "coverage_span": coverage["span"],
        "refit_days": config["refit_days"],
        "origins": eras,
        "caveats": [
            "Windows are frozen before model selection. The holdout is consulted once "
            "for release assessment; a consulted holdout is no longer untouched.",
            "Native history is short: at the first validation origin the expanding and "
            "180-day windows differ by two days, so that comparison only separates at "
            "later origins.",
            "2025-09-30 is a time-grid fixture but lies in no fold; the hourly holdout "
            "ends in 2024 as PLAN.md proposes.",
        ],
    }
    atomic_json(Path("reports/splits.json"), report)
    print(f"frozen {config['sha256'][:12]}: {json.dumps(eras)}")


if __name__ == "__main__":
    main()
