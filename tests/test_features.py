from datetime import date, timedelta

from test_backtest import seed_days

from power_forecast.backtest import load_archive
from power_forecast.baseline import slot_lookup
from power_forecast.features import (
    FEATURES,
    austrian_holidays,
    day_features,
    easter,
    feature_names,
)
from power_forecast.ingestion.entsoe import Price


def test_easter_and_austrian_holidays():
    known = {2019: (4, 21), 2022: (4, 17), 2024: (3, 31), 2025: (4, 20), 2026: (4, 5)}
    for year, (month, day) in known.items():
        assert easter(year) == date(year, month, day)
    holidays = austrian_holidays(2026)
    assert len(holidays) == 13
    assert {date(2026, 4, 6), date(2026, 5, 14), date(2026, 6, 4), date(2026, 10, 26)} <= holidays


def test_features_ignore_results_unavailable_at_cutoff(tmp_path):
    target = date(2026, 9, 17)
    seed_days(tmp_path, target - timedelta(days=21), target + timedelta(days=2))
    archive = load_archive(tmp_path)
    past = {day: value for day, value in archive.items() if day < target}
    rows = day_features(archive, slot_lookup(archive.values()), target)
    assert rows == day_features(past, slot_lookup(past.values()), target)
    assert len(rows) == 96 and all(len(row) == len(FEATURES) for row in rows)
    previous = [r.eur_mwh for r in archive[target - timedelta(days=1)][1]]
    assert [row[FEATURES.index("lag_1")] for row in rows] == previous


def test_missing_spring_slot_takes_the_prior_day_value(tmp_path):
    target = date(2026, 4, 5)  # one week after the 92-interval spring day
    seed_days(tmp_path, target - timedelta(days=21), target - timedelta(days=1))
    archive = load_archive(tmp_path)
    rows = day_features(archive, slot_lookup(archive.values()), target)
    two_am = [row for row in rows if row[FEATURES.index("hour")] == 2]
    lag_7, base = FEATURES.index("lag_7"), FEATURES.index("base")
    assert len(two_am) == 4 and all(row[lag_7] == row[base] for row in two_am)
    assert day_features({}, {}, target) is None


def test_de_lu_group_is_point_in_time_and_falls_back_to_at(tmp_path):
    target = date(2026, 9, 17)
    seed_days(tmp_path / "at", target - timedelta(days=21), target + timedelta(days=1))
    seed_days(tmp_path / "de", target - timedelta(days=21), target + timedelta(days=1))
    at = load_archive(tmp_path / "at")
    de = {
        day: (record, [Price(r.start, r.minutes, r.eur_mwh + 50, "de", "1") for r in rows])
        for day, (record, rows) in load_archive(tmp_path / "de").items()
    }
    lookup = slot_lookup(at.values())
    names = feature_names(("de_lu",))

    def features(de_archive):
        return day_features(
            at, lookup, target, {"de_lu": (de_archive, slot_lookup(de_archive.values()))}
        )

    rows = features(de)
    assert all(len(row) == len(names) for row in rows)
    assert rows == features({d: v for d, v in de.items() if d < target})
    assert all(row[names.index("spread_prev_mean")] == -50 for row in rows)
    assert all(row[names.index("de_lag_1")] == row[names.index("lag_1")] + 50 for row in rows)
    yesterday = target - timedelta(days=1)
    gap = features({d: v for d, v in de.items() if d != yesterday})
    for row in gap:
        assert row[names.index("de_lag_1")] == row[names.index("lag_1")]
        assert row[names.index("spread_prev_mean")] == 0.0
