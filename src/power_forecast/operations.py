"""Single-writer batch jobs, durable attempts and one official forecast per day."""

import fcntl
import json
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import sleep
from uuid import uuid4

from power_forecast.baseline import forecast
from power_forecast.config import api_key
from power_forecast.history import TRANSITION, atomic_json, ingest_day, ingest_window
from power_forecast.ingestion.entsoe import SourceError
from power_forecast.ingestion.observations import observe, observe_weather
from power_forecast.lifecycle import CANDIDATE, shadow_forecast
from power_forecast.scoring import score
from power_forecast.timegrid import VIENNA, forecast_cutoff, results_expected


def utcnow() -> datetime:
    return datetime.now(UTC)


@contextmanager
def writer_lock(data_dir: Path):
    """The OS releases this lock after crashes; never delete the lock file."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".writer.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SourceError("Another writer is active; retry this job later.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def official_path(data_dir: Path, day: date) -> Path:
    return data_dir / "official" / f"{day}.json"


def run_shadow(day: date, data_dir: Path) -> dict:
    """The candidate's forecast, made after the official one; it never fails publish."""
    try:
        record = shadow_forecast(day, data_dir, utcnow())
        return {"model": record["model"], "status": "reused" if record.get("reused") else "saved"}
    except Exception as error:  # noqa: BLE001 - the shadow must not affect publication
        return {"status": "error", "error_type": type(error).__name__}


def publish(day: date, data_dir: Path) -> dict:
    pointer = official_path(data_dir, day)
    if pointer.exists():
        existing = json.loads(pointer.read_text())
        if not (data_dir / existing["forecast_path"]).is_file():
            raise SourceError("Official forecast artifact is missing; restore from backup.")
        return existing | {"reused": True, "shadow": run_shadow(day, data_dir)}
    now = utcnow()
    cutoff = forecast_cutoff(day)
    if not cutoff <= now <= cutoff + timedelta(minutes=30):
        raise SourceError("Publication is allowed only from 10:30 through 11:00 Vienna on D-1.")
    artifact = forecast(day, data_dir, now=now)
    published = utcnow()
    if published > cutoff + timedelta(minutes=30):
        raise SourceError("Publication window closed while computing; artifact remains diagnostic.")
    record = {
        "delivery_date": str(day),
        "forecast_path": str(artifact.relative_to(data_dir)),
        "published_at": published.isoformat(),
        "status": "on_time" if published <= cutoff + timedelta(minutes=15) else "late",
    }
    atomic_json(pointer, record)
    return record | {"shadow": run_shadow(day, data_dir)}


def fill_training_history(day: date, key: str, data_dir: Path) -> dict:
    """Download missing days the candidate trains on, a month per request.

    Covers the training window plus the two weeks its lag features reach back, older
    than the 28 days collect maintains. Once filled, nothing is requested.
    """
    first = max(TRANSITION, day - timedelta(days=CANDIDATE["training_window"] + 15))
    missing = []
    source_day = first
    while source_day < day - timedelta(days=28):
        if not (data_dir / "prices" / "days" / f"{source_day}.json").exists():
            missing.append(source_day)
        source_day += timedelta(days=1)
    downloaded = 0
    for start in range(0, len(missing), 31):
        downloaded += len(ingest_window(missing[start : start + 31], key, data_dir))
        sleep(0.5)
    return {"missing": len(missing), "downloaded": downloaded, "status": "success"}


def collect(day: date, data_dir: Path) -> dict:
    # At 10:00 on D-1, D-1 auction prices already exist. Bootstrap the 28-day
    # fallback window; refresh only the latest three days on subsequent runs.
    key = api_key()
    completed, failures = [], []
    for lag in range(1, 29):
        source_day = day - timedelta(days=lag)
        try:
            result = ingest_day(source_day, key, data_dir, refresh=lag <= 3)
            completed.append(str(source_day))
            if not result["cached"]:
                sleep(0.5)
        except (SourceError, ValueError, OSError) as error:
            failures.append({"day": str(source_day), "error_type": type(error).__name__})
    try:
        training_history = fill_training_history(day, key, data_dir)
    except Exception as error:  # noqa: BLE001 - research history must not fail collection
        training_history = {"status": "error", "error_type": type(error).__name__}
    # Availability evidence for candidate covariates; it never affects this job's status.
    try:
        observations = [
            {"document": e["document"], "status": e["status"]} for e in observe(day, key, data_dir)
        ]
    except Exception as error:  # noqa: BLE001 - evidence collection must not fail collection
        observations = [{"status": "error", "error_type": type(error).__name__}]
    try:
        observations.append(
            {"document": "weather", "status": observe_weather(day, data_dir)["status"]}
        )
    except Exception as error:  # noqa: BLE001 - evidence collection must not fail collection
        observations.append(
            {"document": "weather", "status": "error", "error_type": type(error).__name__}
        )
    return {
        "completed": completed,
        "failures": failures,
        "observations": observations,
        "training_history": training_history,
        "status": "partial" if failures else "success",
    }


def settle(day: date, data_dir: Path) -> dict:
    key = api_key()
    now = utcnow()
    results = []
    # Retry every unscored official day, and refresh three recent days for revisions.
    scored = {
        json.loads(path.read_text())["forecast_run_id"]
        for path in (data_dir / "scores").glob("*.json")
    }
    for pointer in sorted((data_dir / "official").glob("*.json")):
        record = json.loads(pointer.read_text())
        target = date.fromisoformat(record["delivery_date"])
        if target > day:
            continue
        run = data_dir / record["forecast_path"]
        if target < day - timedelta(days=2) and run.stem in scored:
            continue
        try:
            ingest_day(target, key, data_dir, refresh=True)
            output = score(run, data_dir)
            results.append(
                {
                    "day": str(target),
                    "status": "scored",
                    "score_path": str(output.relative_to(data_dir)),
                }
            )
        except (SourceError, ValueError, OSError) as error:
            # Only a day that should be scorable and still has no score is a failure.
            if run.stem in scored:
                status = "kept_previous_score"
            elif target == day and now < results_expected(day):
                status = "awaiting_results"
            else:
                status = "pending_or_failed"
            results.append(
                {"day": str(target), "status": status, "error_type": type(error).__name__}
            )
        sleep(0.5)
    return {
        "results": results,
        "shadow": settle_shadow(day, data_dir, scored),
        "status": (
            "partial"
            if any(item["status"] == "pending_or_failed" for item in results)
            else "success"
        ),
    }


def settle_shadow(day: date, data_dir: Path, scored: set) -> list[dict]:
    """Score shadow forecasts against the actuals the official loop just refreshed.

    Failures are reported but never make settle partial.
    """
    results = []
    for pointer in sorted((data_dir / "shadow" / "days").glob("*/*.json")):
        record = json.loads(pointer.read_text())
        target = date.fromisoformat(record["delivery_date"])
        run = data_dir / record["forecast_path"]
        if target > day or (target < day - timedelta(days=2) and run.stem in scored):
            continue
        try:
            score(run, data_dir)
            results.append({"model": record["model"], "day": str(target), "status": "scored"})
        except (SourceError, ValueError, OSError) as error:
            results.append(
                {
                    "model": record["model"],
                    "day": str(target),
                    "status": "pending",
                    "error_type": type(error).__name__,
                }
            )
    return results


def health(day: date, data_dir: Path) -> dict:
    now = utcnow()
    pointer = official_path(data_dir, day)
    publication = json.loads(pointer.read_text()) if pointer.exists() else None
    issues = []
    if now > forecast_cutoff(day) + timedelta(minutes=15):
        if publication is None:
            issues.append("missing_forecast")
        elif publication["status"] != "on_time":
            issues.append("late_forecast")
    if publication and not (data_dir / publication["forecast_path"]).is_file():
        issues.append("missing_artifact")
    if now > forecast_cutoff(day) + timedelta(hours=4):
        scored = {
            json.loads(path.read_text())["forecast_run_id"]
            for path in (data_dir / "scores").glob("*.json")
        }
        for path in (data_dir / "official").glob("*.json"):
            record = json.loads(path.read_text())
            if date.fromisoformat(record["delivery_date"]) <= day:
                if Path(record["forecast_path"]).stem not in scored:
                    issues.append("pending_scores")
                    break
    attempts = []
    for path in (data_dir / "operations" / "attempts").glob("*.json"):
        value = json.loads(path.read_text())
        if value["delivery_date"] == str(day):
            attempts.append(value)
    if now >= forecast_cutoff(day):
        collection = [a for a in attempts if a["job"] == "collect"]
        if not collection:
            issues.append("missing_collection")
        elif max(collection, key=lambda a: a["started_at"])["status"] != "success":
            issues.append("collection_failed_or_incomplete")
    return {
        "delivery_date": str(day),
        "checked_at": now.isoformat(),
        "publication": publication,
        "issues": issues,
        "status": "unhealthy" if issues else "healthy",
    }


def run_job(job: str, data_dir: Path, day: date | None = None) -> dict:
    if job not in {"collect", "publish", "settle", "watchdog", "dashboard"}:
        raise ValueError("Unknown batch job.")
    day = day or (utcnow().astimezone(VIENNA).date() + timedelta(days=1))
    with writer_lock(data_dir):
        attempt = {
            "attempt_id": uuid4().hex,
            "job": job,
            "delivery_date": str(day),
            "started_at": utcnow().isoformat(),
            "status": "running",
        }
        path = data_dir / "operations" / "attempts" / f"{attempt['attempt_id']}.json"
        atomic_json(path, attempt)
        try:
            action = {
                "collect": collect,
                "publish": publish,
                "settle": settle,
                "watchdog": health,
                "dashboard": export_dashboard,
            }[job]
            result = action(day, data_dir)
            status = result.get("status", "success")
            attempt.update(
                result=result,
                status=(
                    "success" if status in {"success", "healthy", "on_time", "late"} else status
                ),
            )
        except Exception as error:
            # Never persist arbitrary exception strings that could contain credentials.
            attempt.update(status="failed", error_type=type(error).__name__)
            raise
        except BaseException:
            attempt.update(status="interrupted")
            raise
        finally:
            attempt["finished_at"] = utcnow().isoformat()
            atomic_json(path, attempt)
            atomic_json(data_dir / "operations" / f"{job}.json", attempt)
        return attempt


def export_dashboard(day: date, data_dir: Path) -> dict:
    from power_forecast.dashboard import build_dashboard

    build_dashboard(data_dir)
    return {"status": "success"}
