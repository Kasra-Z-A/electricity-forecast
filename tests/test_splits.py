import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from power_forecast.splits import check_coverage, last_label_day, load_splits, rolling_origins
from power_forecast.timegrid import forecast_cutoff, results_expected

CONFIG = Path(__file__).parents[1] / "configs" / "splits.json"


@pytest.fixture
def config():
    return load_splits(CONFIG)


def write(tmp_path, change):
    value = json.loads(CONFIG.read_text())
    change(value)
    path = tmp_path / "splits.json"
    path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize("era", ["hourly", "native"])
@pytest.mark.parametrize("fold", ["validation", "holdout"])
@pytest.mark.parametrize("window", ["expanding", 180])
def test_origins_train_only_on_labels_available_at_cutoff(config, era, fold, window):
    origins = rolling_origins(config, era, fold, window)
    spec = config["eras"][era]
    evaluated = []
    for origin in origins:
        train_first, train_last = (date.fromisoformat(d) for d in origin["train"])
        first, last = (date.fromisoformat(d) for d in origin["evaluate"])
        assert results_expected(train_last) <= forecast_cutoff(first)
        assert results_expected(train_last + timedelta(days=1)) > forecast_cutoff(first)
        assert train_last < first
        assert train_first >= date.fromisoformat(spec["train"][0])
        if window != "expanding":
            assert (train_last - train_first).days < window
        evaluated += [first + timedelta(days=n) for n in range((last - first).days + 1)]
    fold_first, fold_last = (date.fromisoformat(d) for d in spec[fold])
    assert evaluated == [
        fold_first + timedelta(days=n) for n in range((fold_last - fold_first).days + 1)
    ]


def test_label_lag_is_one_day_across_dst(config):
    for day in [date(2025, 10, 26), date(2025, 10, 27), date(2026, 3, 29), date(2026, 3, 30)]:
        assert last_label_day(day) == day - timedelta(days=1)


def test_native_folds_start_at_resolution_transition(config):
    assert config["eras"]["native"]["train"][0] == "2025-10-01"
    assert config["eras"]["hourly"]["holdout"][1] < "2025-10-01"


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda c: c["eras"]["native"].update(validation=["2026-03-31", "2026-06-30"]), "contig"),
        (lambda c: c["eras"]["hourly"].update(holdout=["2024-01-01", "2025-10-05"]), "resolution"),
        (lambda c: c["eras"]["native"].update(prospective_from="2026-08-31"), "prospective"),
        (lambda c: c.update(training_windows=["recent"]), "training window"),
    ],
)
def test_invalid_configurations_are_refused(tmp_path, change, message):
    with pytest.raises(ValueError, match=message):
        load_splits(write(tmp_path, change))


def test_coverage_check(config):
    report = json.loads((CONFIG.parents[1] / "reports" / "coverage.json").read_text())
    assert check_coverage(config, report) == []
    short = report | {"span": {"first": "2019-01-01", "last": "2026-09-17"}}
    assert check_coverage(config, short) == [
        "hourly windows 2018-10-01..2024-12-31 exceed the archive"
    ]
    assert check_coverage(config, report | {"complete": False}) == [
        "coverage report is not complete"
    ]


def test_checksum_pins_config(tmp_path, config):
    changed = load_splits(write(tmp_path, lambda c: c.update(refit_days=14)))
    assert changed["sha256"] != config["sha256"]
