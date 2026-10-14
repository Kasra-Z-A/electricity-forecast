"""Frozen evaluation windows and rolling-origin folds for backtests.

Every interval of a delivery day belongs to the same fold. A fold refits at the
forecast cutoff of its first evaluation day and trains only on days whose auction
result was available by then, so the same rule that governs live settlement also
bounds what a backtest may learn from.
"""

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

from power_forecast.history import resolution
from power_forecast.timegrid import forecast_cutoff, results_expected

FOLDS = ("train", "validation", "holdout")


def _window(value: list[str]) -> tuple[date, date]:
    first, last = (date.fromisoformat(item) for item in value)
    if first > last:
        raise ValueError(f"Window {value} ends before it starts.")
    return first, last


def load_splits(path: Path) -> dict:
    """Read and validate a split configuration; returns it with its checksum."""
    raw = path.read_bytes()
    config = json.loads(raw)
    if config["refit_days"] < 1:
        raise ValueError("refit_days must be positive.")
    for window in config["training_windows"]:
        if window != "expanding" and not (isinstance(window, int) and window > 0):
            raise ValueError(f"Unknown training window {window!r}.")
    for name, era in config["eras"].items():
        windows = [_window(era[fold]) for fold in FOLDS]
        for (_, previous_last), (first, _) in zip(windows, windows[1:]):
            if first != previous_last + timedelta(days=1):
                raise ValueError(f"{name}: folds must be ordered and contiguous.")
        for first, last in windows:
            if {resolution(first), resolution(last)} != {era["resolution_minutes"]}:
                raise ValueError(f"{name}: windows cross the resolution transition.")
        holdout_last = windows[-1][1]
        prospective = era.get("prospective_from")
        if prospective and date.fromisoformat(prospective) <= holdout_last:
            raise ValueError(f"{name}: prospective evidence overlaps the holdout.")
        for slice_name, value in era.get("slices", {}).items():
            first, last = _window(value)
            if first < windows[0][0] or last > holdout_last:
                raise ValueError(f"{name}: slice {slice_name} lies outside the era.")
    return config | {"sha256": hashlib.sha256(raw).hexdigest()}


def last_label_day(cutoff_day: date) -> date:
    """Latest delivery day whose result is available at `cutoff_day`'s cutoff."""
    day = cutoff_day
    while results_expected(day) > forecast_cutoff(cutoff_day):
        day -= timedelta(days=1)
    return day


def rolling_origins(config: dict, era: str, fold: str, training_window) -> list[dict]:
    """Weekly refits across `fold`, each evaluated on the days until the next refit."""
    if fold not in FOLDS[1:]:
        raise ValueError("Rolling origins evaluate validation or holdout folds.")
    spec = config["eras"][era]
    era_first = _window(spec["train"])[0]
    first, last = _window(spec[fold])
    step = timedelta(days=config["refit_days"])
    origins = []
    start = first
    while start <= last:
        end = min(start + step - timedelta(days=1), last)
        train_last = last_label_day(start)
        train_first = (
            era_first
            if training_window == "expanding"
            else max(era_first, train_last - timedelta(days=training_window - 1))
        )
        origins.append(
            {
                "cutoff": forecast_cutoff(start).isoformat(),
                "train": [str(train_first), str(train_last)],
                "evaluate": [str(start), str(end)],
            }
        )
        start = end + timedelta(days=1)
    return origins


def check_coverage(config: dict, report: dict) -> list[str]:
    """Problems that stop a frozen split from being used with this archive."""
    problems = []
    if not report.get("complete"):
        problems.append("coverage report is not complete")
    span = report.get("span") or {}
    archive_first = date.fromisoformat(span.get("first", "9999-12-31"))
    archive_last = date.fromisoformat(span.get("last", "0001-01-01"))
    for name, era in config["eras"].items():
        first, last = _window(era["train"])[0], _window(era["holdout"])[1]
        if first < archive_first or last > archive_last:
            problems.append(f"{name} windows {first}..{last} exceed the archive")
    # Time-grid fixtures must be archived; 2025-09-30 deliberately sits in no fold.
    for value in config["fixture_days"]:
        if not archive_first <= date.fromisoformat(value) <= archive_last:
            problems.append(f"fixture day {value} is not archived")
    return problems
