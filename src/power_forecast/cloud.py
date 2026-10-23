"""AWS Lambda entry point: one batch job per invocation on state kept in S3.

The data directory lives in S3 as one compressed snapshot. A run takes a lock
object, downloads the snapshot into /tmp, runs the job and the dashboard export,
uploads the new snapshot and publishes the dashboard. The lock is created with a
conditional write, so two overlapping runs cannot both hold it; a run that finds
it held exits and the next scheduled attempt retries, as the host's file lock did.
A lock older than any Lambda run can be is taken over, again conditionally. The
snapshot itself is replaced only if it is still the version this run read.

In staging mode the snapshot and dashboard stay under `staging/` in the state
bucket and nothing is published.
"""

import io
import json
import mimetypes
import os
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

JOBS = ("collect", "publish", "settle", "watchdog")
# Lambda stops a run after at most 15 minutes, so an older lock has no live owner.
STALE_LOCK = timedelta(minutes=16)
DATA_DIR = Path("/tmp/data")
EXCLUDED = {"dashboard", ".writer.lock"}


class Busy(Exception):
    """Another run holds the lock."""


def _settings() -> dict:
    mode = os.environ["PIPELINE_MODE"]
    if mode not in ("staging", "live"):
        raise ValueError("PIPELINE_MODE must be staging or live.")
    return {
        "mode": mode,
        "bucket": os.environ["STATE_BUCKET"],
        "site_bucket": os.environ.get("SITE_BUCKET", ""),
        "token_parameter": os.environ["TOKEN_PARAMETER"],
        "schedule_group": os.environ["SCHEDULE_GROUP"],
    }


def _precondition_failed(error) -> bool:
    code = getattr(error, "response", {}).get("Error", {}).get("Code")
    return code in ("PreconditionFailed", "ConditionalRequestConflict")


def acquire(s3, bucket: str, key: str, now: datetime) -> str:
    """Create the lock object, or take over a stale one; returns its ETag."""
    body = json.dumps({"taken_at": now.isoformat()}).encode()
    try:
        return s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*")["ETag"]
    except Exception as error:
        if not _precondition_failed(error):
            raise
    held = s3.head_object(Bucket=bucket, Key=key)
    if now - held["LastModified"] < STALE_LOCK:
        raise Busy
    try:
        return s3.put_object(Bucket=bucket, Key=key, Body=body, IfMatch=held["ETag"])["ETag"]
    except Exception as error:
        if _precondition_failed(error):
            raise Busy from None
        raise


def download(s3, bucket: str, key: str, data_dir: Path) -> str | None:
    """Unpack the snapshot into an empty `data_dir`; returns its ETag, or None if absent."""
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True)
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") == "NoSuchKey":
            return None
        raise
    with tarfile.open(fileobj=io.BytesIO(response["Body"].read()), mode="r:gz") as archive:
        archive.extractall(data_dir, filter="data")
    return response["ETag"]


def pack(data_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(data_dir.iterdir()):
            if path.name not in EXCLUDED:
                archive.add(path, arcname=path.name)
    return buffer.getvalue()


def upload(s3, bucket: str, key: str, data_dir: Path, etag: str | None) -> None:
    """Replace the snapshot only if it is still the version that was downloaded."""
    condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
    s3.put_object(Bucket=bucket, Key=key, Body=pack(data_dir), **condition)


def publish(s3, bucket: str, prefix: str, dashboard: Path) -> None:
    """Upload the dashboard export; data and pages are cached briefly, assets longer."""
    for path in sorted(p for p in dashboard.rglob("*") if p.is_file()):
        name = path.relative_to(dashboard).as_posix()
        short = name in ("dashboard.json", "index.html")
        s3.put_object(
            Bucket=bucket,
            Key=prefix + name,
            Body=path.read_bytes(),
            ContentType=mimetypes.guess_type(name)[0] or "application/octet-stream",
            CacheControl="public,max-age=60" if short else "public,max-age=300",
        )


def record_scheduler(scheduler, group: str, data_dir: Path, now: datetime) -> None:
    """Whether every schedule in the group is enabled, for the dashboard's status line."""
    from power_forecast.history import atomic_json

    states = [
        schedule["State"]
        for page in scheduler.get_paginator("list_schedules").paginate(GroupName=group)
        for schedule in page["Schedules"]
    ]
    active = bool(states) and all(state == "ENABLED" for state in states)
    atomic_json(
        data_dir / "operations" / "scheduler.json",
        {"active": active, "checked_at": now.isoformat()},
    )


def handler(event, context=None, client=None) -> dict:
    """Run `event["job"]`; `client` makes AWS clients by service name (boto3 by default)."""
    from power_forecast.operations import run_job

    if client is None:
        import boto3

        client = boto3.client
    job = event.get("job")
    if job not in JOBS:
        raise ValueError(f"Unknown job {job!r}.")
    settings = _settings()
    mode, bucket = settings["mode"], settings["bucket"]
    s3 = client("s3")
    lock_key, state_key = f"{mode}/lock", f"{mode}/state.tar.gz"
    try:
        acquire(s3, bucket, lock_key, datetime.now(UTC))
    except Busy:
        print(json.dumps({"job": job, "status": "busy"}))
        return {"job": job, "status": "busy"}
    failure = None
    try:
        etag = download(s3, bucket, state_key, DATA_DIR)
        record_scheduler(
            client("scheduler"), settings["schedule_group"], DATA_DIR, datetime.now(UTC)
        )
        if job in ("collect", "settle"):
            parameter = client("ssm").get_parameter(
                Name=settings["token_parameter"], WithDecryption=True
            )
            os.environ["ENTSOE_API_KEY"] = parameter["Parameter"]["Value"]
        try:
            status = run_job(job, DATA_DIR)["status"]
        except Exception as error:  # noqa: BLE001 - recorded by run_job; re-raised below
            failure, status = error, "failed"
        finally:
            os.environ.pop("ENTSOE_API_KEY", None)
        # Export fresh status even when the job failed, as the host did; the
        # snapshot is saved even if the export fails.
        try:
            run_job("dashboard", DATA_DIR)
        except Exception as error:  # noqa: BLE001 - recorded by run_job; re-raised below
            failure = failure or error
        upload(s3, bucket, state_key, DATA_DIR, etag)
        if (DATA_DIR / "dashboard" / "dashboard.json").exists():
            if mode == "live":
                publish(s3, settings["site_bucket"], "", DATA_DIR / "dashboard")
            else:
                publish(s3, bucket, "staging/dashboard/", DATA_DIR / "dashboard")
    finally:
        s3.delete_object(Bucket=bucket, Key=lock_key)
    print(json.dumps({"job": job, "status": status, "mode": mode}))
    if failure is not None:
        raise failure
    return {"job": job, "status": status}
