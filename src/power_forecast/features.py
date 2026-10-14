"""Point-in-time feature rows for learned models (PLAN.md section 7).

Every value for delivery day D comes from days whose result was available at D's
cutoff, found through the same local clock-slot matching the baselines use.
Missing lagged slots (spring DST, archive gaps) take the prior-day baseline value,
a deterministic rule that needs no statistics fitted on any data.
"""

from datetime import date, timedelta
from math import cos, pi, sin
from statistics import mean

from power_forecast.baseline import DAILY_LAGS, matching_slot
from power_forecast.history import resolution
from power_forecast.splits import last_label_day
from power_forecast.timegrid import VIENNA, delivery_grid

LAGS = (1, 2, 7, 14)
CATEGORICAL = ("hour", "weekday")
FEATURES = (
    "hour",
    "weekday",
    "quarter",
    "weekend",
    "holiday",
    "doy_sin",
    "doy_cos",
    "utc_offset",
    "base",
    *(f"lag_{lag}" for lag in LAGS),
    "prev_mean",
    "prev_min",
    "prev_max",
    "week_mean",
)


def easter(year: int) -> date:
    """Gregorian Easter Sunday (Meeus/Jones/Butcher algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def austrian_holidays(year: int) -> set[date]:
    """Nationwide statutory public holidays (Feiertagsruhegesetz)."""
    sunday = easter(year)
    fixed = [(1, 1), (1, 6), (5, 1), (8, 15), (10, 26), (11, 1), (12, 8), (12, 25), (12, 26)]
    movable = [1, 39, 50, 60]  # Easter Monday, Ascension, Whit Monday, Corpus Christi
    return {date(year, m, d) for m, d in fixed} | {sunday + timedelta(days=n) for n in movable}


def _day_prices(archive: dict, day: date) -> list[float]:
    return [row.eur_mwh for row in archive[day][1]] if day in archive else []


def day_features(archive: dict, lookup: dict, day: date) -> list[list[float]] | None:
    """One feature row per delivery interval of `day`, or None without eligible history."""
    newest = last_label_day(day)
    previous = _day_prices(archive, newest)
    week = [p for n in range(7) for p in _day_prices(archive, newest - timedelta(days=n))]
    if not previous or not week:
        return None
    holidays = austrian_holidays(day.year)
    year_days = 366 if day.year % 4 == 0 else 365
    angle = 2 * pi * (day.timetuple().tm_yday - 1) / year_days
    rows = []
    for target in delivery_grid(day, resolution(day)):
        local = target.astimezone(VIENNA)

        def lagged(lag):
            if day - timedelta(days=lag) > newest:
                return None
            match = matching_slot(lookup, day, target, lag)
            return match[0].eur_mwh if match else None

        base = next((v for lag in DAILY_LAGS if (v := lagged(lag)) is not None), None)
        if base is None:
            return None
        lags = [lagged(lag) for lag in LAGS]
        rows.append(
            [
                local.hour,
                local.weekday(),
                local.minute // 15,
                float(local.weekday() >= 5),
                float(day in holidays),
                sin(angle),
                cos(angle),
                local.utcoffset().total_seconds() / 3600,
                base,
                *(base if value is None else value for value in lags),
                mean(previous),
                min(previous),
                max(previous),
                mean(week),
            ]
        )
    return rows
