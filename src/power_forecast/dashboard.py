"""Export a public, read-only dashboard without exposing the private archive."""

import hashlib
import json
import math
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from power_forecast.history import atomic_json, load_version
from power_forecast.promotion import monitoring
from power_forecast.timegrid import VIENNA, forecast_cutoff

VERSIONED_ASSETS = ("tokens.css", "dashboard.css", "theme.js", "dashboard.js")


def dashboard_data(data_dir: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    tomorrow = str(now.astimezone(VIENNA).date() + timedelta(days=1))
    next_delivery = now.astimezone(VIENNA).date() + timedelta(days=1)
    if now >= forecast_cutoff(next_delivery):
        next_delivery += timedelta(days=1)
    scheduler_path = data_dir / "operations/scheduler.json"
    scheduler = json.loads(scheduler_path.read_text()) if scheduler_path.exists() else {}
    official = {}
    for path in (data_dir / "official").glob("*.json"):
        value = json.loads(path.read_text())
        official[Path(value["forecast_path"]).stem] = value
    selected = {}
    for path in (data_dir / "forecasts").glob("*.json"):
        run = json.loads(path.read_text())
        body = {k: v for k, v in run.items() if k != "run_id"}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if digest != run["run_id"]:
            raise ValueError("Cannot export a forecast with an invalid checksum.")
        # Every artifact is checksum-verified, but only official forecasts are shown.
        if run["run_id"] not in official:
            continue
        if run["mode"] != "live":
            raise ValueError("An official publication must reference a live forecast.")
        selected[run["delivery_date"]] = run
    curves = []
    for day, run in sorted(selected.items(), reverse=True):
        actual = {}
        retrieved = None
        checkpoint = data_dir / "prices/days" / f"{day}.json"
        if checkpoint.exists():
            record = json.loads(checkpoint.read_text())
            _, rows = load_version(data_dir, record)
            actual = {row.start.isoformat(): row.eur_mwh for row in rows}
            retrieved = record["retrieved_at"]
        points = [
            {
                "start": p["start"],
                "minutes": p["minutes"],
                "forecast": p["point"],
                "actual": actual.get(p["start"]),
                "fallback": p["fallback"],
            }
            for p in run["predictions"]
        ]
        errors = [p["forecast"] - p["actual"] for p in points if p["actual"] is not None]
        complete = len(errors) == len(points) and bool(points)
        publication = official[run["run_id"]]
        curves.append(
            {
                "date": day,
                "model": run["model"],
                "issued_at": run["issued_at"],
                "cutoff_at": run["cutoff_at"],
                "published_at": publication["published_at"],
                "status": publication["status"],
                "actual_retrieved_at": retrieved,
                "points": points,
                "mae": sum(abs(e) for e in errors) / len(errors) if complete else None,
                "rmse": math.sqrt(sum(e * e for e in errors) / len(errors)) if complete else None,
            }
        )
    jobs = []
    for job in ("collect", "publish", "settle", "watchdog"):
        path = data_dir / "operations" / f"{job}.json"
        attempt = json.loads(path.read_text()) if path.exists() else {}
        jobs.append(
            {
                "job": job,
                "status": attempt.get("status", "not_started"),
                "finished_at": attempt.get("finished_at"),
                "delivery_date": attempt.get("delivery_date"),
            }
        )
    return {
        "generated_at": now.isoformat(),
        "tomorrow": tomorrow,
        "curves": curves,
        "jobs": jobs,
        "scheduler_active": scheduler.get("active", False),
        "scheduler_checked_at": scheduler.get("checked_at"),
        "next_forecast_at": forecast_cutoff(next_delivery).isoformat(),
        "next_delivery_date": str(next_delivery),
        "tomorrow_available": any(c["date"] == tomorrow for c in curves),
        "models": monitoring(data_dir),
    }


def build_dashboard(
    data_dir: Path, output_dir: Path | None = None, static_preview: bool = False
) -> Path:
    output_dir = output_dir or data_dir / "dashboard"
    # Resolve before writing: a caller must never export over source artifacts.
    if (
        output_dir.resolve() == data_dir.resolve()
        or output_dir.resolve() in data_dir.resolve().parents
    ):
        raise ValueError("Dashboard output must be separate from the archive root.")
    value = dashboard_data(data_dir)
    value["deployment_mode"] = "snapshot" if static_preview else "scheduled"
    assets = Path(__file__).with_name("dashboard_assets")
    output_dir.mkdir(parents=True, exist_ok=True)
    for source in assets.iterdir():
        if source.is_dir():
            shutil.copytree(source, output_dir / source.name, dirs_exist_ok=True)
        else:
            shutil.copyfile(source, output_dir / source.name)
    # The page is cached briefly and its scripts and styles longer, by browsers and
    # by CloudFront, whose cache key ignores query strings. Each asset is therefore
    # also written under a name with its hash, and the page links to that name, so
    # a changed asset is a new URL at the next page load.
    page = output_dir / "index.html"
    html = page.read_text()
    for name in VERSIONED_ASSETS:
        source = output_dir / name
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
        hashed = f"{source.stem}.{digest}{source.suffix}"
        shutil.copyfile(source, output_dir / hashed)
        html = html.replace(f'"{name}"', f'"{hashed}"')
    page.write_text(html)
    atomic_json(output_dir / "dashboard.json", value)
    return output_dir
