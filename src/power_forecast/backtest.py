"""Rolling-origin backtests of the seasonal baselines over frozen split windows.

The archive is loaded once and every day is forecast from matching local clock
slots, so thousands of days run without writing forecast artifacts. History for a
day is limited to days whose result was available at that day's cutoff, the same
rule the splits use. Inputs are the latest archived versions, not vintages
retrieved before each past cutoff; results are a retrospective reconstruction.
"""

from datetime import date, timedelta
from pathlib import Path
from statistics import median

from power_forecast.baseline import DAILY_LAGS, WEEKLY_LAGS, matching_slot, slot_lookup
from power_forecast.coverage import checkpoints
from power_forecast.history import load_version, resolution
from power_forecast.ingestion.entsoe import Price
from power_forecast.metrics import quantile
from power_forecast.splits import last_label_day
from power_forecast.timegrid import VIENNA, delivery_grid

MEDIAN_LAGS = (7, 14, 21, 28)


def load_archive(data_dir: Path) -> dict[date, tuple[dict, list[Price]]]:
    """Every checkpointed day, checksum-verified on load."""
    return {day: load_version(data_dir, record) for day, record in checkpoints(data_dir).items()}


def _first_match(lookup, day, target, lags, newest):
    for lag in lags:
        if day - timedelta(days=lag) > newest:
            continue
        match = matching_slot(lookup, day, target, lag)
        if match:
            return lag, match[0]
    return None


def predict(strategy: str, lookup: dict, day: date) -> list[dict] | None:
    """Point forecasts for `day`, or None if any interval has no eligible history."""
    newest = last_label_day(day)
    minutes = resolution(day)
    points = []
    for target in delivery_grid(day, minutes):
        if strategy == "weekday_median":
            rows = [
                match[0]
                for lag in MEDIAN_LAGS
                if day - timedelta(days=lag) <= newest
                and (match := matching_slot(lookup, day, target, lag))
            ]
            if rows:
                points.append(
                    {
                        "point": median(row.eur_mwh for row in rows),
                        "fallback": len(rows) < len(MEDIAN_LAGS)
                        or any(row.minutes != minutes for row in rows),
                    }
                )
                continue
            # No same-weekday slot at all: borrow the weekly rule, always a fallback.
            found = _first_match(lookup, day, target, WEEKLY_LAGS, newest)
            if found is None:
                return None
            points.append({"point": found[1].eur_mwh, "fallback": True})
            continue
        lags = {"weekly": WEEKLY_LAGS, "prior_day": DAILY_LAGS}[strategy]
        found = _first_match(lookup, day, target, lags, newest)
        if found is None:
            return None
        lag, row = found
        points.append({"point": row.eur_mwh, "fallback": lag != lags[0] or row.minutes != minutes})
    return points


def trailing_high_price(archive: dict, day: date, q: float = 0.95, days: int = 28) -> float:
    """The `q` quantile of the `days` results available at `day`'s cutoff."""
    newest = last_label_day(day)
    values = [
        row.eur_mwh
        for lag in range(days)
        if (source := newest - timedelta(days=lag)) in archive
        for row in archive[source][1]
    ]
    return quantile(values, q)


def backtest(archive: dict, days: list[date], strategies: list[str]) -> dict[str, list[dict]]:
    """Per strategy, one record per evaluated day with interval-level errors."""
    lookup = slot_lookup([(record, rows) for record, rows in archive.values()])
    results = {name: [] for name in strategies}
    for day in days:
        _, actuals = archive[day]
        for name in strategies:
            points = predict(name, lookup, day)
            if points is None:
                results[name].append({"day": day, "intervals": None})
                continue
            results[name].append(
                {
                    "day": day,
                    "intervals": [
                        {
                            "local": actual.start.astimezone(VIENNA),
                            "actual": actual.eur_mwh,
                            "error": point["point"] - actual.eur_mwh,
                            "fallback": point["fallback"],
                        }
                        for point, actual in zip(points, actuals, strict=True)
                    ],
                }
            )
    return results
