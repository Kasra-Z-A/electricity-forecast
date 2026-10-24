"""The champion pointer, the automatic promotion gate and rollback (docs/DESIGN.md, Evaluation and promotion).

`models/champion.json` names the model whose forecast is published; without it the
seasonal baseline is champion. Settle evaluates every candidate after scoring; of
those that pass every check, the one with the largest skill over the champion is
promoted automatically from the next publication. Every
change is appended to `models/events.jsonl`. A rolled-back model is blocked: only a
new candidate version can be promoted again.

All models are compared on the same days against the latest complete actuals. The
champion's and weekly baseline's forecasts come from their official publications or,
where they were not official, from vintage replays of what they would have published.
"""

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from power_forecast.baseline import MODEL_INFO, PRODUCTION_MODEL
from power_forecast.history import atomic_json, load_version
from power_forecast.ingestion.entsoe import SourceError
from power_forecast.lifecycle import (
    CANDIDATE,
    CANDIDATE_INFO,
    CHALLENGER_INFO,
    CHALLENGERS,
    RETIRED,
    pointer_path,
    reference_forecast,
)
from power_forecast.metrics import _errors, _point_metrics, paired_comparison

WEEKLY = "seasonal-weekly-v1"
# Engineering defaults from docs/DESIGN.md, not scientific constants.
POLICY = {
    "min_common_days": 28,
    "min_live_shadow_days": 7,
    "min_mae_improvement": 0.02,
    "max_rmse_worsening": 0.05,
    # A challenger with this many live shadow days whose interval lies entirely above
    # zero is confidently worse than the champion and is flagged for retirement.
    "retire_after_live_days": 28,
}


def champion(data_dir: Path) -> dict:
    path = data_dir / "models" / "champion.json"
    return json.loads(path.read_text()) if path.exists() else {"model": PRODUCTION_MODEL}


def events(data_dir: Path) -> list[dict]:
    path = data_dir / "models" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _record(data_dir: Path, event: dict, pointer: dict) -> None:
    atomic_json(data_dir / "models" / "champion.json", pointer)
    with (data_dir / "models" / "events.jsonl").open("a") as log:
        log.write(json.dumps(event) + "\n")


def _load_run(path: Path) -> dict:
    run = json.loads(path.read_text())
    body = {k: v for k, v in run.items() if k != "run_id"}
    if hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != run["run_id"]:
        raise SourceError(f"Forecast checksum mismatch: {path.name}.")
    return run


def _forecast_path(data_dir: Path, model: str, day: date, replay: bool = True) -> Path | None:
    """The official forecast if `model` published it, else its shadow or replay.

    With `replay`, a missing seasonal baseline is replayed from vintage versions.
    """
    official = data_dir / "official" / f"{day}.json"
    if official.exists():
        path = data_dir / json.loads(official.read_text())["forecast_path"]
        if _load_run(path)["model"] == model:
            return path
    pointer = pointer_path(data_dir, model, day)
    if replay and not pointer.exists() and model in (PRODUCTION_MODEL, WEEKLY):
        try:
            reference_forecast(day, data_dir, model)
        except SourceError:
            return None  # no eligible history at that cutoff
    if pointer.exists():
        return data_dir / json.loads(pointer.read_text())["forecast_path"]
    return None


def _day_errors(data_dir: Path, path: Path, actual: dict) -> dict:
    run = _load_run(path)
    if [p["start"] for p in run["predictions"]] != list(actual):
        raise SourceError(f"Forecast grid does not match actuals for {run['delivery_date']}.")
    return {
        "day": date.fromisoformat(run["delivery_date"]),
        "intervals": [{"error": p["point"] - actual[p["start"]]} for p in run["predictions"]],
    }


def candidates() -> tuple:
    """Every model the gate may promote: the shadow candidate and the challengers."""
    return tuple(name for name in (CANDIDATE["name"], *CHALLENGERS) if name not in RETIRED)


def _blocked(data_dir: Path) -> set:
    return {e["model"] for e in events(data_dir) if e["event"] == "rollback"}


def evaluate(data_dir: Path, now: datetime, name: str | None = None, tested: int = 1) -> dict:
    """Compare one candidate with the champion on every common scorable day.

    `tested` is how many candidates this gate run evaluates; the interval check uses
    a Bonferroni-corrected level, 1 - 0.05 / tested, so testing several candidates
    does not raise the chance of promoting one that is not better.
    """
    name, current = name or CANDIDATE["name"], champion(data_dir)["model"]
    level = 1 - 0.05 / tested
    candidate, reference, weekly, live_days = [], [], [], 0
    for pointer in sorted((data_dir / "shadow" / "days" / name).glob("*.json")):
        record = json.loads(pointer.read_text())
        day = date.fromisoformat(record["delivery_date"])
        checkpoint = data_dir / "prices" / "days" / f"{day}.json"
        if not checkpoint.exists():
            continue  # actuals pending
        _, rows = load_version(data_dir, json.loads(checkpoint.read_text()))
        actual = {row.start.isoformat(): row.eur_mwh for row in rows}
        paths = [_forecast_path(data_dir, model, day) for model in (current, WEEKLY)]
        if None in paths:
            continue
        candidate.append(_day_errors(data_dir, data_dir / record["forecast_path"], actual))
        reference.append(_day_errors(data_dir, paths[0], actual))
        weekly.append(_day_errors(data_dir, paths[1], actual))
        live_days += record["mode"] == "live"
    metrics = {
        label: _point_metrics(_errors(days))
        for label, days in (("candidate", candidate), ("champion", reference), ("weekly", weekly))
    }
    comparison = (
        paired_comparison(candidate, reference, level=level) if candidate else {"common_days": 0}
    )
    interval = comparison.get("difference_interval", comparison.get("difference_95"))
    if not candidate:
        interval = None
    cand, champ, week = (metrics[k] for k in ("candidate", "champion", "weekly"))
    enough = len(candidate) >= POLICY["min_common_days"]
    checks = {
        "common_days": enough,
        "live_shadow_days": live_days >= POLICY["min_live_shadow_days"],
        "mae_improvement": enough
        and cand["mae"] <= (1 - POLICY["min_mae_improvement"]) * champ["mae"],
        "beats_weekly": enough and cand["mae"] < week["mae"],
        "interval_below_zero": enough and interval[1] < 0,
        "rmse_guard": enough and cand["rmse"] <= (1 + POLICY["max_rmse_worsening"]) * champ["rmse"],
        "not_rolled_back": name not in _blocked(data_dir),
    }
    report = {
        "evaluated_at": now.isoformat(),
        "candidate": name,
        "champion": current,
        "policy": POLICY,
        "candidates_tested": tested,
        "interval_level": level,
        "first_day": str(candidate[0]["day"]) if candidate else None,
        "last_day": str(candidate[-1]["day"]) if candidate else None,
        "live_shadow_days": live_days,
        "metrics": metrics,
        "versus_champion": comparison,
        "checks": checks,
        "passed": all(checks.values()),
        "retire": live_days >= POLICY["retire_after_live_days"]
        and interval is not None
        and interval[0] > 0,
    }
    digest = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    path = data_dir / "models" / "evaluations" / f"{now:%Y-%m-%d}-{digest[:12]}.json"
    atomic_json(path, report)
    return report | {"path": str(path.relative_to(data_dir))}


def run_gate(data_dir: Path, now: datetime) -> dict:
    """Evaluate every candidate; promote the one that passed with the largest skill.

    Called by settle. The champion and rolled-back models are not evaluated.
    """
    current = champion(data_dir)["model"]
    blocked = _blocked(data_dir)
    names = [name for name in candidates() if name != current and name not in blocked]
    if not names:
        return {"status": "no_candidates"}
    results = {}
    for name in names:
        report = evaluate(data_dir, now, name, tested=len(names))
        results[name] = {
            "evaluation": report["path"],
            "common_days": report["versus_champion"]["common_days"],
            "live_shadow_days": report["live_shadow_days"],
            "failed_checks": [check for check, ok in report["checks"].items() if not ok],
            "mae_skill": report["versus_champion"].get("mae_skill"),
            "retire": report["retire"],
        }
    passed = [name for name in names if not results[name]["failed_checks"]]
    if not passed:
        return {"status": "kept_champion", "candidates": results}
    best = max(passed, key=lambda name: results[name]["mae_skill"])
    promote(data_dir, best, results[best]["evaluation"], now)
    return {"status": "promoted", "promoted": best, "candidates": results}


def promote(data_dir: Path, model: str, evidence: str, now: datetime) -> None:
    previous = champion(data_dir)
    event = {
        "event": "promote",
        "model": model,
        "previous": previous["model"],
        "evidence": evidence,
        "at": now.isoformat(),
    }
    _record(data_dir, event, {"model": model, "since": now.isoformat(), "previous": previous})


def rollback(data_dir: Path, now: datetime, reason: str) -> dict:
    """Restore the previous champion and block the current one from promotion."""
    current = champion(data_dir)
    if "previous" not in current:
        raise SourceError(f"{current['model']} has no previous champion to roll back to.")
    event = {
        "event": "rollback",
        "model": current["model"],
        "restored": current["previous"]["model"],
        "reason": reason,
        "at": now.isoformat(),
    }
    _record(data_dir, event, current["previous"])
    return event


def _actuals(data_dir: Path, day: date) -> dict | None:
    checkpoint = data_dir / "prices" / "days" / f"{day}.json"
    if not checkpoint.exists():
        return None
    _, rows = load_version(data_dir, json.loads(checkpoint.read_text()))
    return {row.start.isoformat(): row.eur_mwh for row in rows}


def monitoring(data_dir: Path) -> dict:
    """Read-only model comparison for the dashboard: recent errors on common days.

    Windows count back over days on which the published model and both seasonal
    baselines have a forecast and complete actuals. A candidate's value covers the
    same days; a window in which it lacks one of them shows no value.
    """
    current = champion(data_dir)["model"]
    shadow = [name for name in candidates() if name != current]
    baselines = [m for m in (PRODUCTION_MODEL, WEEKLY) if m != current]
    required = [current, *baselines]
    days = {date.fromisoformat(p.stem) for p in (data_dir / "official").glob("*.json")}
    for name in shadow:
        days |= {
            date.fromisoformat(p.stem) for p in (data_dir / "shadow" / "days" / name).glob("*.json")
        }
    common = []
    for day in sorted(days, reverse=True):
        actual = _actuals(data_dir, day)
        paths = [_forecast_path(data_dir, m, day, replay=False) for m in required]
        if actual is None or None in paths:
            continue
        try:
            errors = {m: _day_errors(data_dir, path, actual) for m, path in zip(required, paths)}
        except SourceError:
            continue  # actuals incomplete or a grid mismatch: not comparable
        common.append((day, errors, actual))
        if len(common) == 28:
            break

    def errors_of(model, day, known, actual):
        if model in known:
            return known[model]
        path = _forecast_path(data_dir, model, day, replay=False)
        try:
            return _day_errors(data_dir, path, actual) if path else None
        except SourceError:
            return None

    rows = []
    for model in [current, *shadow, *baselines]:
        if model == current:
            role = "published"
        elif model in shadow:
            role = "shadow"
        else:
            role = "fallback" if model == PRODUCTION_MODEL else "baseline"
        row = {"model": model, "role": role}
        for window in (7, 28):
            chosen = [
                errors_of(model, day, known, actual) for day, known, actual in common[:window]
            ]
            complete = bool(chosen) and None not in chosen
            row[f"mae_{window}"] = _point_metrics(_errors(chosen))["mae"] if complete else None
        rows.append(row)
    return {
        "champion": current,
        "candidates": shadow,
        "fallback": PRODUCTION_MODEL,
        "catalog": MODEL_INFO | {CANDIDATE["name"]: CANDIDATE_INFO} | CHALLENGER_INFO,
        "policy": POLICY,
        "rows": rows,
    }
