"""Evaluation metrics shared by every model family (docs/DESIGN.md, Evaluation and promotion).

Quarter-hours within a day are not independent evidence, so uncertainty in a model
comparison comes from a week-block bootstrap of paired daily loss differences.
"""

import math
import random
from collections import defaultdict
from statistics import mean

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _errors(days: list[dict]) -> list[dict]:
    return [item for day in days if day["intervals"] for item in day["intervals"]]


def _point_metrics(items: list[dict]) -> dict:
    if not items:
        return {"intervals": 0, "mae": None, "rmse": None}
    return {
        "intervals": len(items),
        "mae": mean(abs(i["error"]) for i in items),
        "rmse": math.sqrt(mean(i["error"] ** 2 for i in items)),
    }


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(days: list[dict], high_price: dict) -> dict:
    """Interval-weighted and daily-mean metrics, breakdowns and preregistered slices.

    `high_price` maps each day to its own threshold, so a level shift between
    training and evaluation cannot turn most of a fold into the "high" slice.
    """
    available = [day for day in days if day["intervals"]]
    items = _errors(available)
    by_hour, by_weekday = defaultdict(list), defaultdict(list)
    for item in items:
        by_hour[item["local"].hour].append(item)
        by_weekday[WEEKDAYS[item["local"].weekday()]].append(item)
    return {
        "days": len(days),
        "unavailable_days": len(days) - len(available),
        **_point_metrics(items),
        "daily_mean_mae": mean(daily_mae(day) for day in available) if available else None,
        "fallback_rate": mean(i["fallback"] for i in items) if items else None,
        "by_local_hour": {h: _point_metrics(v) for h, v in sorted(by_hour.items())},
        "by_weekday": {d: _point_metrics(by_weekday[d]) for d in WEEKDAYS if d in by_weekday},
        "slices": {
            "negative_price": _point_metrics([i for i in items if i["actual"] < 0]),
            "high_price": _point_metrics(
                [
                    i
                    for day in available
                    for i in day["intervals"]
                    if i["actual"] >= high_price[day["day"]]
                ]
            ),
        },
    }


def daily_mae(day: dict) -> float:
    return mean(abs(i["error"]) for i in day["intervals"])


def paired_comparison(
    candidate: list[dict], reference: list[dict], replicates: int = 2000, seed: int = 0
) -> dict:
    """Candidate minus reference daily MAE on common days, with a week-block 95% interval.

    Negative differences favour the candidate. Skill is 1 - candidate/reference MAE,
    undefined when the reference MAE is zero.
    """
    ref = {day["day"]: day for day in reference if day["intervals"]}
    pairs = [(day, ref[day["day"]]) for day in candidate if day["intervals"] and day["day"] in ref]
    if not pairs:
        return {"common_days": 0}
    weeks = defaultdict(list)
    for cand, base in pairs:
        weeks[cand["day"].isocalendar()[:2]].append(daily_mae(cand) - daily_mae(base))
    blocks = list(weeks.values())
    rng = random.Random(seed)
    draws = []
    for _ in range(replicates):
        sample = [d for _ in blocks for d in rng.choice(blocks)]
        draws.append(mean(sample))
    candidate_mae = _point_metrics(_errors([c for c, _ in pairs]))["mae"]
    reference_mae = _point_metrics(_errors([b for _, b in pairs]))["mae"]
    return {
        "common_days": len(pairs),
        "week_blocks": len(blocks),
        "mae_skill": None if reference_mae == 0 else 1 - candidate_mae / reference_mae,
        "daily_mae_difference": mean(d for block in blocks for d in block),
        "difference_95": [quantile(draws, 0.025), quantile(draws, 0.975)],
    }


def pinball(actual: float, predicted: float, q: float) -> float:
    difference = actual - predicted
    return max(q * difference, (q - 1) * difference)


def probabilistic_summary(days: list[dict], levels: tuple = (0.1, 0.5, 0.9)) -> dict:
    """Pinball loss per quantile, central-interval coverage and width, crossing rate.

    Coverage is reported overall and by local hour, because a well-calibrated
    average can hide hours where the interval is much too narrow.
    """
    items = _errors([day for day in days if day["intervals"]])
    if not items:
        return {"intervals": 0}
    low, high = levels[0], levels[-1]

    def covered(item):
        return item["quantiles"][0] <= item["actual"] <= item["quantiles"][-1]

    by_hour = defaultdict(list)
    for item in items:
        by_hour[item["local"].hour].append(covered(item))
    hourly = {hour: mean(values) for hour, values in sorted(by_hour.items())}
    return {
        "intervals": len(items),
        "pinball": {
            f"p{round(q * 100)}": mean(pinball(i["actual"], i["quantiles"][n], q) for i in items)
            for n, q in enumerate(levels)
        },
        "nominal_coverage": round(high - low, 6),
        "coverage": mean(covered(i) for i in items),
        "mean_width": mean(i["quantiles"][-1] - i["quantiles"][0] for i in items),
        "raw_crossing_rate": mean(i.get("crossed", False) for i in items),
        "coverage_by_local_hour": hourly,
        "worst_hour_coverage": min(hourly.values()),
    }
