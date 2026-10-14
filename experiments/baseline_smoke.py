"""Reproduce the short baseline replay check; this is not a model-selection backtest.

Run from the repository root after backfilling the required price dates:
    uv run python experiments/baseline_smoke.py
"""

import json
from datetime import date, timedelta
from pathlib import Path

from power_forecast.baseline import forecast
from power_forecast.history import atomic_json
from power_forecast.scoring import score


def main() -> None:
    results = []
    for offset in range(8):
        day = date(2026, 9, 6) + timedelta(days=offset)
        run = forecast(day, Path("data"), replay=True, model="seasonal-weekly-v1")
        results.append(json.loads(score(run, Path("data")).read_text()))
    summary = {
        "evaluation": "reconstructed replay smoke test, not a held-out model comparison",
        "days": len(results),
        "intervals": sum(r["intervals"] for r in results),
        "mae": sum(r["mae"] for r in results) / len(results),
        "rmse": (sum(r["rmse"] ** 2 for r in results) / len(results)) ** 0.5,
        "fallback_intervals": sum(r["fallback_intervals"] for r in results),
        "daily_results": results,
    }
    atomic_json(Path("reports/baseline_smoke.json"), summary)
    print(
        f"{summary['intervals']} intervals: MAE={summary['mae']:.3f}, RMSE={summary['rmse']:.3f} EUR/MWh"
    )


if __name__ == "__main__":
    main()
