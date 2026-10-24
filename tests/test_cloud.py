import hashlib
import io
import json
import os
import tarfile
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from power_forecast import cloud

NOW = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Objects with ETags and conditional puts, as S3 general purpose buckets behave."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, IfMatch=None, **metadata):
        current = self.objects.get((Bucket, Key))
        if IfNoneMatch == "*" and current is not None:
            raise ClientError("PreconditionFailed")
        if IfMatch is not None and (current is None or current["ETag"] != IfMatch):
            raise ClientError("PreconditionFailed")
        etag = '"' + hashlib.md5(Body + str(len(self.objects)).encode()).hexdigest() + '"'
        self.objects[(Bucket, Key)] = {
            "Body": Body,
            "ETag": etag,
            "LastModified": self.now,
            **metadata,
        }
        return {"ETag": etag}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError("NoSuchKey")
        item = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(item["Body"]), "ETag": item["ETag"]}

    def head_object(self, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        return {"ETag": item["ETag"], "LastModified": item["LastModified"]}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    now = NOW


class FakeScheduler:
    def __init__(self, states):
        self.states = states

    def get_paginator(self, name):
        assert name == "list_schedules"
        return self

    def paginate(self, GroupName):
        return [{"Schedules": [{"State": state} for state in self.states]}]


class FakeSSM:
    def get_parameter(self, Name, WithDecryption):
        assert WithDecryption
        return {"Parameter": {"Value": "token"}}


@pytest.fixture
def aws(tmp_path, monkeypatch):
    s3 = FakeS3()
    # The handler reads the real clock, so objects must be stamped with it too.
    s3.now = datetime.now(UTC)
    clients = {"s3": s3, "scheduler": FakeScheduler(["ENABLED"] * 4), "ssm": FakeSSM()}
    monkeypatch.setattr(cloud, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("PIPELINE_MODE", "live")
    monkeypatch.setenv("STATE_BUCKET", "state")
    monkeypatch.setenv("SITE_BUCKET", "site")
    monkeypatch.setenv("TOKEN_PARAMETER", "/token")
    monkeypatch.setenv("SCHEDULE_GROUP", "group")
    return s3, clients.__getitem__


def test_lock_is_exclusive_until_it_is_stale():
    s3 = FakeS3()
    cloud.acquire(s3, "state", "live/lock", NOW)
    with pytest.raises(cloud.Busy):
        cloud.acquire(s3, "state", "live/lock", NOW + timedelta(minutes=5))
    cloud.acquire(s3, "state", "live/lock", NOW + timedelta(minutes=17))


def test_snapshot_is_replaced_only_from_the_version_read(tmp_path):
    s3 = FakeS3()
    data = tmp_path / "data"
    assert cloud.download(s3, "state", "live/state.tar.gz", data) is None
    (data / "prices").mkdir()
    (data / "prices" / "a.json").write_text("{}")
    (data / ".writer.lock").write_text("")
    cloud.upload(s3, "state", "live/state.tar.gz", data, None)
    etag = cloud.download(s3, "state", "live/state.tar.gz", data)
    assert (data / "prices" / "a.json").read_text() == "{}"
    assert not (data / ".writer.lock").exists()
    cloud.upload(s3, "state", "live/state.tar.gz", data, etag)
    with pytest.raises(ClientError):
        cloud.upload(s3, "state", "live/state.tar.gz", data, etag)


def test_a_run_publishes_the_dashboard_and_releases_the_lock(aws):
    s3, client = aws
    with patch("power_forecast.operations.run_job", return_value={"status": "success"}) as job:
        job.side_effect = lambda name, data_dir: _export(name, data_dir)
        assert cloud.handler({"job": "watchdog"}, client=client)["status"] == "success"
    assert ("state", "live/lock") not in s3.objects
    assert s3.objects[("site", "dashboard.json")]["CacheControl"] == "public,max-age=60"
    assert s3.objects[("site", "dashboard.json")]["ContentType"] == "application/json"
    snapshot = s3.objects[("state", "live/state.tar.gz")]["Body"]
    with tarfile.open(fileobj=io.BytesIO(snapshot), mode="r:gz") as archive:
        names = archive.getnames()
    assert "operations" in names and "dashboard" not in names
    scheduler = json.loads((cloud.DATA_DIR / "operations" / "scheduler.json").read_text())
    assert scheduler["active"] is True


def test_a_failed_job_still_saves_state_then_reports_the_error(aws):
    s3, client = aws

    def run(name, data_dir):
        if name == "collect":
            assert os.environ["ENTSOE_API_KEY"] == "token"
            raise RuntimeError("source down")
        return _export(name, data_dir)

    with patch("power_forecast.operations.run_job", side_effect=run):
        with pytest.raises(RuntimeError):
            cloud.handler({"job": "collect"}, client=client)
    assert ("state", "live/state.tar.gz") in s3.objects
    assert ("state", "live/lock") not in s3.objects
    assert "ENTSOE_API_KEY" not in os.environ


def test_a_held_lock_skips_the_run_and_staging_never_publishes(aws, monkeypatch):
    s3, client = aws
    s3.put_object(Bucket="state", Key="live/lock", Body=b"{}")
    with patch("power_forecast.operations.run_job") as job:
        assert cloud.handler({"job": "publish"}, client=client)["status"] == "busy"
        job.assert_not_called()
    monkeypatch.setenv("PIPELINE_MODE", "staging")
    with patch("power_forecast.operations.run_job", side_effect=_export):
        cloud.handler({"job": "publish"}, client=client)
    assert ("state", "staging/dashboard/dashboard.json") in s3.objects
    assert not any(bucket == "site" for bucket, _ in s3.objects)


def _export(name, data_dir):
    (data_dir / "operations").mkdir(parents=True, exist_ok=True)
    (data_dir / "operations" / f"{name}.json").write_text("{}")
    if name == "dashboard":
        (data_dir / "dashboard").mkdir(exist_ok=True)
        (data_dir / "dashboard" / "dashboard.json").write_text("{}")
    return {"status": "success"}
