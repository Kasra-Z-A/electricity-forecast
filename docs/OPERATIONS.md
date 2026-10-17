# Daily baseline operation

Daily jobs are deployed on an EC2 instance in Frankfurt.
All six systemd timers are enabled. Docker Compose runs the Python batch image;
the host uploads the public export to S3/CloudFront at https://power.kasra.page.
Deployment began after the September 16 cutoff. The first official forecast attempt
is September 17, 2026 at 10:30 Europe/Vienna, for delivery on September 18.

Verified on September 16: real collection succeeded, 36 delivery days were cached,
the private S3 backup was restored and checksum-validated, and Docker restart
recovery preserved data and active timers. Official forecasts use `seasonal-daily-v1`
from delivery day October 16, 2026; earlier official days used `seasonal-weekly-v1`.
The dashboard's error metrics cover only the newest day's model. By October 14, 28 consecutive scheduled
attempts (delivery September 18 to October 15) had published on time and been scored;
see reports/m2_observation_2026-10-14.json. No learned model or automatic model
promotion is running.

## Commands

Run from the project root after `uv sync --locked`:

```sh
uv run power-forecast job collect
uv run power-forecast job publish
uv run power-forecast job settle
uv run power-forecast job watchdog
```

Every job defaults to tomorrow's delivery date in Europe/Vienna. Use
`--delivery-date YYYY-MM-DD` to inspect or retry a specific date and `--data-dir`
to select a separate archive. Job output contains no credentials. Partial, failed
and unhealthy outcomes exit nonzero. Inspect `operations/attempts/` under the data
directory for each attempt, and `operations/JOB.json` for its latest outcome.

| Vienna time on D-1 | Job | Behavior |
|---|---|---|
| 10:00, 10:15 | collect | Bootstrap the preceding 28 delivery days; refresh the latest three for revisions |
| 10:30–10:55, every five minutes | publish | Publish once; later attempts reuse the official pointer |
| 13:00, 14:00, 16:00, 20:00 | settle | Retrieve actual auction prices and score; retry unscored days and refresh three recent days |
| 10:46, 11:05, 20:15 | watchdog | Report missing/late publication, missing artifacts, unsuccessful collection and pending scores |

Collection continues past individual day failures and records their dates. Each
collection also fetches the ENTSO-E day-ahead load forecast (A65) and wind/solar
forecast (A69) for the delivery day and appends what it found to
`observations/load_forecast.jsonl` and `observations/wind_solar_forecast.jsonl`.
It also archives the ECMWF 00 UTC run of D-1 from Open-Meteo under `weather/` once
it is served, and logs each attempt to `observations/weather.jsonl`; a run archived
at 10:00 shows as cached at 10:15 with its first retrieval time.
This is availability evidence for the covariate audit only; it never feeds a forecast
and its failures never change the collection status. Existing
eligible history can still support publication. Historical data downloaded after
10:30 cannot enter that day's official forecast. Publication is allowed only from
10:30 through 11:00 on D-1; publication after 10:45 is explicitly late. Backfilled
historical forecasts must use the separate `forecast --replay` command.

Collection also downloads any day missing from the candidate's 180-day training
window (plus two weeks of lag history), a month per request; once filled it requests
nothing. Failures are reported under `training_history` and never fail collection.

After the official forecast is published, publish runs the M6 candidate
(`ridge-core-180-v1`, see `lifecycle.py`) in shadow: it refits at most weekly on the
labels available at the cutoff and forecasts from the same eligible versions as the
official run. Shadow artifacts are written under `shadow/`, never `forecasts/`, so they
cannot reach the public dashboard or the official pointer, and their errors never
change the publish status. Settle scores them after the official days.

`power-forecast shadow-replay START END` recreates candidate forecasts for past days
from the versions retrieved before each cutoff; run it on the host archive, which
collected those versions live. Its fits use later-downloaded training labels and are
marked `reconstructed_labels`.

### Champion, automatic promotion and rollback

`models/champion.json` names the published model; without it the champion is
`seasonal-daily-v1`. When the candidate is champion, publish writes its forecast as
the official one; if that fails for any reason, the seasonal baseline is published
instead and the pointer records `fallback_from`.

Every settle run evaluates the candidate (`promotion.py`) on all days it has a
scorable shadow or replay forecast, against the champion and `seasonal-weekly-v1` on
the same days and the latest actuals. Where those baselines were not official, the
gate replays what they would have published from the versions retrieved before each
cutoff. The candidate is promoted automatically, effective from the next publication,
when all checks pass: at least 28 common days, at least 14 of them live shadow days,
MAE at least 2% below the champion, below the weekly baseline, the week-block 95%
interval of the daily MAE difference entirely below zero, and RMSE no more than 5%
above the champion. Each evaluation is kept under `models/evaluations/`; promotions
and rollbacks are appended to `models/events.jsonl`. Gate errors never fail settle.

`power-forecast evaluate` runs the gate without promoting. One-off commands such as
`rollback`, `evaluate` and `shadow-replay` run on the host as root, holding the host
lock so no scheduled job runs at the same time:

```sh
cd /opt/electricity-forecast && set -a && . /etc/power-forecast/host.env && set +a
touch /run/power-forecast.env
flock /run/power-forecast-host.lock \
  docker compose -f deploy/compose.server.yaml run --rm batch rollback "reason"
```

This restores the previous champion from the next publication and blocks the rolled-back
model; only a new candidate version can be promoted again. It never replaces an
official forecast that was already published.

`official/YYYY-MM-DD.json` is the authoritative publication pointer. It is atomically
written after the complete forecast artifact. Retries never replace it. A crash
before pointer creation may leave a diagnostic artifact; retrying safely creates
the official pointer. Source snapshots remain immutable and selected by cutoff.

All CLI writers share a nonblocking OS file lock in `data/.writer.lock`. A competing
job exits and can be retried. The OS releases the lock after process termination;
never delete the lock file. Direct Python API callers must take `writer_lock` around
writes. Use a local filesystem, not a network filesystem. Storage is still JSON.

## Linux host setup

Shared AWS resources belong in `aws-infra`. These instructions prepare the project
container on an existing Docker/Compose/systemd host; they do not provision one.

1. Put the project at `/opt/electricity-forecast`, or change the service template's
   working directory. Configure `.env` securely on the host with `ENTSOE_API_KEY`.
   Keep secrets out of images and source control.
2. Ensure `data/` is owned by the UID/GID used by the container (default 1000:1000).
   If different, set `POWER_UID` and `POWER_GID` in the Compose environment file.
3. Build and smoke-test the image:

   ```sh
   docker compose --env-file .env -f deploy/compose.yaml build
   docker compose --env-file .env -f deploy/compose.yaml run --rm batch doctor
   docker compose --env-file .env -f deploy/compose.yaml run --rm batch job collect
   ```

4. Validate the systemd units and calendar expressions on the target host:

   ```sh
   systemd-analyze verify deploy/systemd/*.service deploy/systemd/*.timer
   systemd-analyze calendar '*-*-* 10:00,15:00 Europe/Vienna'
   systemd-analyze calendar '*-*-* 10:30,35,40,45,50,55:00 Europe/Vienna'
   ```

5. Install and activate the timers:

   ```sh
   sudo cp deploy/systemd/* /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now power-forecast-collect.timer power-forecast-publish.timer power-forecast-settle.timer power-forecast-watchdog.timer power-forecast-dashboard.timer
   systemctl list-timers 'power-forecast*'
   ```

Timers use explicit Vienna time and persistent catch-up. A missed publication timer
does not bypass the application's publication window. The timer semantics are
described in the [systemd timer documentation](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml).
The host must maintain accurate time. Image build and unit validation must pass on
the target host before claiming deployment. Container memory/CPU limits are initial
guardrails and still require measurement.

## Recovery and acceptance

- **Source outage:** inspect attempt status; retry `job collect`. Transport retries
  are bounded. If cutoff has passed, newly retrieved inputs remain ineligible for
  that day's publication. Fix the source before tomorrow's collection.
- **Missing token:** configure `.env`, run `doctor`, then retry collection. The
  stored diagnostic records the error type, never arbitrary upstream error text.
- **Writer busy:** wait for the active job, then retry. Do not remove the lock.
- **Crash:** an abruptly killed job can leave a `running` attempt. Check the host
  journal (`journalctl -u 'power-forecast@*'`) and rerun the job. Prior attempts remain.
- **Pending scores:** retry `job settle`; incomplete actual days cannot be scored.
  Errors stay visible and do not prevent other days from being processed.
- **Corrupt snapshot:** use `ingest-prices DATE` to obtain a fresh version. Preserve
  corrupt evidence and restore historical artifacts from backup if needed.
- **Missed publication:** inspect `job watchdog --delivery-date DATE`. Do not
  backdate or replace an official run. Capture the incident in the operating record.

The watchdog writes local diagnostics and exits nonzero. External alerts and host-down
monitoring are not configured. Private S3 backups are active. A powered-off host cannot run its own
watchdog. Historical missed dates can be inspected explicitly after recovery.

M2 acceptance required seven real scheduled delivery-day attempts (met: 28 by
October 14), evidence that prospective snapshots are accumulating (met: new price
versions every day since September 17), and a host outage/retry drill. The drill was
deferred on October 14; the Docker restart recovery of September 16 is the only
recovery evidence, and the offline outage test does not substitute for a real one.
See reports/m2_observation_2026-10-14.json.

Until October 14 the 13:00 settle run was partial every day, because the auction
result for the target day is not yet available from ENTSO-E, and a transient failure
while refreshing an already-scored day also marked a run partial. Both exited nonzero,
so `power-forecast@settle.service` showed as failed while every official day was
scored. Settle now records those cases as `awaiting_results` (target day, before
15:30 Vienna on D-1) and `kept_previous_score`. A run is partial only when an official
day that should be scorable still has no score.

## Dashboard

The first deployment now includes a read-only dashboard. Its typography, palette,
light/dark themes and layout conventions are adapted from `website/src/styles/global.css`
and `website/src/layouts/Base.astro`. Font files are self-hosted; their licenses are
in `src/power_forecast/dashboard_assets/fonts/`. The website repository is unchanged.

Local preview:

```sh
uv run power-forecast dashboard --serve
```

The preview binds to `127.0.0.1:8080`. It is a development server, not the public
server. Only the exported directory is served. The static JSON contains selected
public fields; tokens, source paths, raw responses and detailed errors are excluded.

For deployment, export before starting the dashboard container:

```sh
docker compose --env-file .env -f deploy/compose.yaml run --rm batch job dashboard
docker compose --env-file .env -f deploy/compose.yaml up -d dashboard
```

The dashboard container mounts only the exported directory, read-only, and has no
ENTSO-E credentials. It listens on host loopback port 8080. Connect the shared HTTPS
proxy for `power.kasra.page` through the `aws-infra` deployment. DNS and public TLS
are not configured by these templates. The Nginx setup follows the official
[static content documentation](https://docs.nginx.com/nginx/admin-guide/web-server/serving-static-content/).

The dashboard timer exports every five minutes, offset two minutes from the hour.
If another writer holds the lock it retries at the next scheduled trigger. Failed
exports retain the previous JSON; the page marks exports older than 20 minutes as
stale. The browser also rechecks snapshot age while open. Refresh the page to load
a newly exported snapshot. The background job is included in the timer installation
above. The production Docker batch image is validated; the optional Nginx
container is not used by the S3-hosted deployment.

The dashboard shows official forecasts only: replays, diagnostic runs and shadow
forecasts never appear in its charts, although every artifact in `forecasts/` is
checksum-verified on export. Metrics are recomputed from the latest complete archived
actuals, weighted by interval count, and cover the model behind the newest published
day. They may differ from an older pinned score after a source revision. The model
comparison section reads names and descriptions from `MODEL_INFO` and `CANDIDATE_INFO`,
so promoting a model or starting a new candidate needs no page change. Negative prices and 92/100-interval DST days
are preserved. An empty archive produces a valid empty dashboard rather than sample
or invented prices.

## Initial historical dashboard deployment (superseded by daily updates)

The initial static release used `aws-infra/power-site`: private S3 storage,
CloudFront, ACM and Route 53 for `https://power.kasra.page`. The same hosting now receives updates from EC2. The manual static-preview
command below is retained for historical demonstrations; do not use it to replace
the scheduled production export.

To publish a new export after reviewing local results:

```sh
AWS_PROFILE=admin bash deploy/publish-dashboard.sh \
  power-kasra-page-<account-id> <distribution-id>
```

Read `DISTRIBUTION_ID` from `terraform -chdir=../aws-infra/power-site output -raw
distribution_id`. The script exports with `--static-preview`, uploads only the
public dashboard directory, and invalidates CloudFront. It does not delete old
objects or upload the raw archive. AWS SSO credentials are required. The dashboard
export is still published manually; the application image is not (see below).

For rollback, rebuild and republish the prior reviewed code/data snapshot. There is
not yet an automated release history or one-command rollback. Daily jobs and backups
are now active as described below.

## Running EC2 deployment

The production host uses `deploy/compose.server.yaml`, `deploy/run-job.sh`, and
`deploy/install-server.sh`.

The application image is built once by GitHub Actions in `Kasra-Z-A/electricity-forecast`
and pushed to the ECR repository `power-forecast`, tagged by commit. The host never
builds: `install-server.sh` takes a digest, records it as `POWER_IMAGE` in
`/etc/power-forecast/host.env`, and `run-job.sh` pulls that digest if it is absent.
A reference without `@sha256:` is refused, so a moved tag cannot change what runs.
The image that serves a forecast is therefore the image CI tested. The earlier local Compose/Nginx instructions above are
an alternative local preview setup; the public dashboard continues to use S3.

- Host: one EC2 instance, managed through SSM, no inbound ports.
- Persistent data: `/var/lib/power-forecast`, owned by UID/GID 1000.
- Application: `/opt/electricity-forecast`.
- Non-secret host settings: `/etc/power-forecast/host.env`.
- Token: SSM `/portfolio/power/entsoe-api-key`, read only for collection/scoring,
  temporarily written to root-only `/run/power-forecast.env` and removed on exit.
- Public files: `s3://power-kasra-page-<account-id>`. JSON/HTML cache for 60 seconds;
  assets for 300 seconds. Daily jobs do not issue CloudFront invalidations.
- Private releases/backups: `s3://portfolio-demo-artifacts-<account-id>`.
- Backup: 21:10 Vienna, retained for 30 days under `backups/power/`.
- Scheduler state is observed with systemctl and included in each dashboard export.

The server wrapper serializes jobs with a host lock and refreshes the public page
after each job, including failed outcomes. A separate dashboard timer refreshes it
every five minutes. If a job is blocked by a writer, its next timer attempt retries.
The initial startup day has no official forecast because setup occurred after the
publication window; an unhealthy watchdog result for that day is expected. Start
the consecutive scheduled-day observation window on September 17.

To inspect on the host:

```sh
systemctl list-timers 'power-forecast*'
journalctl -u 'power-forecast@*' --since today
systemctl start power-forecast@collect.service
systemctl start power-forecast@dashboard.service
```

For rollback, stop the timers, re-run `deploy/install-server.sh` with the previous
digest, and restart the timers and dashboard job. Rolling back is repointing
`POWER_IMAGE`, not rebuilding: every digest ECR still retains is a candidate, and
`aws ecr describe-images` lists them. The lifecycle policy keeps ten, so a target
exists from the second push onward; with only one image there is nothing to roll
back to. The images built on the host before ECR were deleted on 2026-09-17, so
the first ECR deployment is the baseline and every image the host can run now
comes from the registry. Do not restore old
data over newer published forecasts as part of a code rollback. Automatic rollback
and external host-down alerts are not configured.

Release packages contain source and deployment scripts, never `.env` or private
credentials. Artifact SHA-256 is verified before extraction. The live diagram marks
manual deployment and planned CI/model-development stages explicitly.
