"""Market days are local calendar days; interval identities are UTC."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

VIENNA = ZoneInfo("Europe/Vienna")


def day_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, VIENNA).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, VIENNA).astimezone(UTC),
    )


def delivery_grid(day: date, minutes: int = 15) -> list[datetime]:
    if minutes not in (15, 60):
        raise ValueError("Resolution must be 15 or 60 minutes.")
    start, end = day_bounds(day)
    step = timedelta(minutes=minutes)
    return [start + i * step for i in range((end - start) // step)]


def forecast_cutoff(day: date) -> datetime:
    return datetime.combine(day - timedelta(days=1), time(10, 30), VIENNA).astimezone(UTC)


def results_expected(day: date) -> datetime:
    """When the auction result for `day` can be relied on from ENTSO-E (15:30 on D-1).

    The auction clears around noon on D-1; this conservative bound is when settle
    starts treating a missing result as a failure and when backtests may use the label.
    """
    return forecast_cutoff(day) + timedelta(hours=5)
