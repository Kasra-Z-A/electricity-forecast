# Daily baseline operation

Daily jobs run as one AWS Lambda function in Frankfurt, started by EventBridge
Scheduler in Europe/Vienna time; each run uploads the public export to
S3/CloudFront at https://power.kasra.page. From September 16 to October 24, 2026 the
same jobs ran on an EC2 instance under systemd timers; the Lambda function took over
on October 24 from a checksum-verified copy of the host archive, after its forecasts
for delivery on October 25 matched the host's in every interval.
Deployment began after the September 16 cutoff. The first official forecast attempt
is September 17, 2026 at 10:30 Europe/Vienna, for delivery on September 18.

Verified on September 16: real collection succeeded, 36 delivery days were cached,
the private S3 backup was restored and checksum-validated, and Docker restart
recovery preserved data and active timers on the host. Official forecasts use `seasonal-daily-v1`
from delivery day October 16, 2026; earlier official days used `seasonal-weekly-v1`.
The dashboard's error metrics cover only the newest day's model. By October 14, 28 consecutive scheduled
attempts (delivery September 18 to October 15) had published on time and been scored;
see reports/m2_observation_2026-10-14.json. The ridge candidate runs in shadow beside
the official forecast and is promoted automatically once it passes the gate (see
below).

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
| 11:10 | challengers | Forecast the delivery day with the challengers in shadow; replay any of the previous 28 days without one |

Collection continues past individual day failures and records their dates. Each
collection also fetches the ENTSO-E day-ahead load forecast (A65) and wind/solar
forecast (A69) for the delivery day and appends what it found to
`observations/load_forecast.jsonl` and `observations/wind_solar_forecast.jsonl`.
It also archives the ECMWF 00 UTC run of D-1 from Open-Meteo under `weather/` once
it is served, and logs each attempt to `observations/weather.jsonl`; a run archived
at 10:00 shows as cached at 10:15 with its first retrieval time.
These logs are availability evidence for the covariate audit, and their failures
never change the collection status. The load forecast for the delivery day is also
archived under `load/` with its retrieval time, reported as `load_forecast`; a day
archived earlier is kept, and a failure leaves the day missing without changing
the collection status. No official forecast uses weather or load. Existing
eligible history can still support publication. Historical data downloaded after
10:30 cannot enter that day's official forecast. Publication is allowed only from
10:30 through 11:00 on D-1; publication after 10:45 is explicitly late. Backfilled
historical forecasts must use the separate `forecast --replay` command.

Collection also downloads any day missing from the candidate's 180-day training
window (plus two weeks of lag history), a month per request; once filled it requests
nothing. Failures are reported under `training_history` and never fail collection.

After the official forecast is published, publish runs the candidate
(`ridge-core-180-v1`, see `lifecycle.py`) in shadow: it refits at most weekly on the
labels available at the cutoff and forecasts from the same eligible versions as the
official run. Shadow artifacts are written under `shadow/`, never `forecasts/`, so they
cannot reach the public dashboard or the official pointer, and their errors never
change the publish status. Settle scores them after the official days.

The `challengers` job (`challengers.py`) forecasts the delivery day with LightGBM,
LEAR and Chronos-2 from price versions, the weather run and the load forecast
retrieved before the cutoff, and writes them under `shadow/` like the ridge shadow.
Chronos-2 runs once per job for all pending days, in a separate process, and also
records its P10 and P90. A challenger that is champion is skipped: publish makes its
forecast. A day whose weather or load forecast arrived after the cutoff gets no live
forecast. Any of the previous 28 days without one is replayed from the versions
retrieved before its cutoff, with training labels and covariates marked
`reconstructed`. Failures are reported per model and day; the job is partial only
when a live forecast fails. The dashboard shows every candidate's errors on the same
days as the published model and the baselines; a window with a missing day shows no
value.

`power-forecast shadow-replay START END` recreates candidate forecasts for past days
from the versions retrieved before each cutoff; run it on the production archive,
which collected those versions live. Its fits use later-downloaded training labels and are
marked `reconstructed_labels`.

### Champion, automatic promotion and rollback

`models/champion.json` names the published model; without it the champion is
`seasonal-daily-v1`. When a candidate is champion, publish writes its forecast as
the official one; if that fails for any reason, the seasonal baseline is published
instead and the pointer records `fallback_from`. A Chronos-2 champion falls back to
LEAR first, then to the seasonal baseline.

Every settle run evaluates each candidate (`promotion.py`): the ridge candidate and
the challengers, except the champion and rolled-back models. Each is compared on all
days it has a scorable shadow or replay forecast, against the champion and
`seasonal-weekly-v1` on the same days and the latest actuals. Where those baselines
were not official, the gate replays what they would have published from the versions
retrieved before each cutoff. A candidate passes when all checks hold: at least 28
common days, at least 7 of them live shadow days, MAE at least 2% below the
champion, below the weekly baseline, the week-block interval of the daily MAE
difference entirely below zero, and RMSE no more than 5% above the champion. The
interval level is Bonferroni-corrected for the number of candidates tested, 1 − 0.05
/ k (98.75% for four), so testing several does not raise the chance of promoting one
by luck. Of the candidates that pass, the one with the largest MAE skill over the
champion is promoted automatically, effective from the next publication. Each
evaluation is kept under `models/evaluations/`; promotions and rollbacks are
appended to `models/events.jsonl`. Gate errors never fail settle.

`power-forecast evaluate` runs the gate without promoting. The one-off commands
`rollback`, `evaluate` and `shadow-replay` run on the production state through the
function's `command` job, which holds the state lock so no scheduled job runs at the
same time, and returns the command's output:

```sh
aws lambda invoke --function-name power-pipeline --cli-binary-format raw-in-base64-out \
  --payload '{"job": "command", "args": ["rollback", "reason"]}' out.json && cat out.json
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
writes. Use a local filesystem, not a network filesystem. Storage is JSON. In the
Lambda deployment the S3 lock object below plays the same role across runs.

## Recovery and acceptance

- **Source outage:** inspect attempt status; retry `job collect`. Transport retries
  are bounded. If cutoff has passed, newly retrieved inputs remain ineligible for
  that day's publication. Fix the source before tomorrow's collection.
- **Missing token:** configure `.env`, run `doctor`, then retry collection. The
  stored diagnostic records the error type, never arbitrary upstream error text.
- **Writer busy:** wait for the active job, then retry. Do not remove the lock.
- **Crash:** an abruptly killed job can leave a `running` attempt. Check the
  function's logs (`aws logs tail /aws/lambda/power-pipeline --since 1d`) and rerun
  the job. Prior attempts remain.
- **Pending scores:** retry `job settle`; incomplete actual days cannot be scored.
  Errors stay visible and do not prevent other days from being processed.
- **Corrupt snapshot:** use `ingest-prices DATE` to obtain a fresh version. Preserve
  corrupt evidence and restore historical artifacts from backup if needed.
- **Missed publication:** inspect `job watchdog --delivery-date DATE`. Do not
  backdate or replace an official run. Capture the incident in the operating record.

The watchdog records its diagnostics and exits nonzero. External alerts are not
configured. Missed dates can be inspected explicitly after recovery.

By October 14, 28 consecutive scheduled delivery-day attempts had published on
time, and new price versions had been archived every day since September 17. No
outage drill has been run: the Docker restart recovery of September 16 is the only
recovery evidence, and the offline outage test does not substitute for a real one. See reports/m2_observation_2026-10-14.json.

Until October 14 the 13:00 settle run was partial every day, because the auction
result for the target day is not yet available from ENTSO-E, and a transient failure
while refreshing an already-scored day also marked a run partial. Both exited nonzero,
so the settle service showed as failed while every official day was scored. Settle now records those cases as `awaiting_results` (target day, before
15:30 Vienna on D-1) and `kept_previous_score`. A run is partial only when an official
day that should be scorable still has no score.

## Dashboard

The dashboard is read-only. Its typography, palette,
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

The `dashboard` schedule exports every five minutes, offset two minutes from the
hour. If another run holds the lock it retries at the next scheduled trigger. Failed
exports retain the previous JSON; the page marks exports older than 20 minutes as
stale. The browser also rechecks snapshot age while open. Refresh the page to load
a newly exported snapshot.

The dashboard shows official forecasts only: replays, diagnostic runs and shadow
forecasts never appear in its charts, although every artifact in `forecasts/` is
checksum-verified on export. Metrics are recomputed from the latest complete archived
actuals, weighted by interval count, and cover the model behind the newest published
day. They may differ from an older pinned score after a source revision. The model
comparison section reads names and descriptions from `MODEL_INFO` and `CANDIDATE_INFO`,
so promoting a model or starting a new candidate needs no page change. Negative prices and 92/100-interval DST days
are preserved. An empty archive produces a valid empty dashboard rather than sample
or invented prices.

## Lambda deployment

Infrastructure comes from the `power-pipeline` stack in `aws-infra`; this repository
holds the handler (`src/power_forecast/cloud.py`), its image (`Dockerfile.lambda`) and
`deploy/deploy-lambda.sh`.

- Functions: `power-pipeline` (1024 MB) runs collect, settle, watchdog and the
  dashboard refresh; `power-pipeline-models` (3008 MB) runs publish and
  challengers, which run the models. Both run the same image with a 15-minute
  timeout, one job per invocation (`{"job": "collect"}` and so on) and no automatic
  retries, and share the state lock.
- Schedules: the EventBridge Scheduler group `power-pipeline` runs the times in the
  table above in Europe/Vienna, so daylight-saving changes move with the market day,
  plus the five-minute `dashboard` refresh.
- State: `s3://power-pipeline-state-<account-id>/live/state.tar.gz`, the whole data
  directory without the dashboard export. A run creates `live/lock` with a
  conditional write, downloads and unpacks the snapshot, runs the job and the
  dashboard export, and uploads the snapshot only if it is still the version it
  downloaded. A run that finds the lock held exits as `busy`; a lock older than 16
  minutes has no live owner and is taken over. The `dashboard` refresh publishes a
  new export without uploading the snapshot.
- Backup: bucket versioning keeps every replaced snapshot for 30 days.
- Token: SSM `/portfolio/power/entsoe-api-key`, read only for collection and
  scoring, and removed from the environment when the job ends.
- Public files: `s3://power-kasra-page-<account-id>`. JSON/HTML cache for 60 seconds;
  assets for 300 seconds. Daily jobs do not issue CloudFront invalidations.
- Scheduler state: each run lists the schedule group and records whether every
  schedule is enabled; the dashboard shows it.
- Logs: CloudWatch Logs `/aws/lambda/power-pipeline` and
  `/aws/lambda/power-pipeline-models`, kept for 30 days.
- Chronos-2 weights: downloaded at the pinned revision when the image is built,
  checked against their SHA-256 and loaded from `CHRONOS_MODEL_DIR`; CI loads them
  once in every image it builds.

The image is built once by GitHub Actions in `Kasra-Z-A/electricity-forecast` and
pushed to the ECR repository `power-pipeline`, tagged by commit. To deploy, pass the
`sha256:` digest from the CI run summary:

```sh
AWS_PROFILE=admin deploy/deploy-lambda.sh sha256:...
```

Anything that is not a digest is refused, so a moved tag cannot change what runs.
To run a job by hand, invoke the function with its name:

```sh
aws lambda invoke --function-name power-pipeline --cli-binary-format raw-in-base64-out \
  --payload '{"job": "watchdog"}' out.json && cat out.json
aws logs tail /aws/lambda/power-pipeline --since 1d
```

For a code rollback, deploy an earlier digest; `aws ecr describe-images
--repository-name power-pipeline` lists them, and the lifecycle policy keeps three.
To restore data, copy an earlier version of `live/state.tar.gz` over the current one
while no run holds the lock. Do not restore old data over newer published forecasts
as part of a code rollback. Automatic code rollback is not configured.
