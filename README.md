# Austrian Day-Ahead Electricity Price Forecasting

A production ML portfolio project for forecasting Austrian SDAC electricity prices
before the day-ahead auction: reproducible ingestion, point-in-time features,
rolling backtests, scheduled batch forecasts, serving and monitored model promotion.

**Status:** resumable price ingestion, seasonal baseline forecasts and scoring are
implemented. ENTSO-E authentication and live historical/DST samples passed.
36 delivery days, including historical transition samples, are cached on the server; the full 2018-onward archive is not yet downloaded.
Batch collection, official publication, scoring and watchdog jobs are implemented,
with Docker/Compose and Linux timer templates. Timers are active on EC2; database
serving and learned models remain planned. A read-only dashboard matching the
portfolio website is implemented for the first deployment.

Read the [full implementation plan](docs/PLAN.md) for the architecture, data contracts,
modeling strategy, evaluation gates, milestones and first implementation batch.

The production target is native 15-minute prices. Older hourly history is a separate
research track. Inputs freeze at 10:30 Europe/Vienna on the day before delivery;
publication is due by 10:45. The first release uses seasonal baselines, then evaluates
LightGBM and optional covariates against the same observations.

## Local setup

Python 3.12 and uv are the intended development tools. Dependencies are pinned in uv.lock.

```bash
uv sync
# Only if .env does not already exist:
cp -n .env.example .env
# Set ENTSOE_API_KEY locally in .env; never commit it.
```

## Implementation sequence

1. Validate API access, source metadata and DST-aware interval grids.
2. Archive prices, generate baseline forecasts and score them end to end.
3. Schedule daily collection and preserve input snapshots.
4. Backtest models, audit optional features and evaluate uncertainty.
5. Expand the early dashboard with a full API, database, backups and monitoring.
6. Add shadow evaluation, gated promotion and rollback; publish measured results.

See the [milestones and acceptance gates](docs/PLAN.md#10-milestones-and-acceptance-gates).
An eight-day reconstructed baseline smoke test produced MAE **34.84 EUR/MWh**
and RMSE **48.72 EUR/MWh** over 768 intervals (September 6–13, 2026). This verifies
the pipeline; it is not a held-out model comparison or evidence of predictive skill.
See [the report](reports/baseline_smoke.json). Deployment templates exist; daily jobs are deployed on EC2.

**Validation backtest (M3).** Weekly-refit rolling origins on frozen windows, MAE in
EUR/MWh, holdouts untouched ([report](reports/backtest_models.json)):

| | Hourly 2021–2023 | Native Apr–Jun 2026 |
|---|---|---|
| Weekly baseline (skill reference) | 40.92 | 37.05 |
| Prior-day baseline (production) | 29.34 | 30.42 |
| Ridge, expanding window | **25.30** | 26.56 |
| Ridge, 180-day window | 26.45 | **26.49** |
| LightGBM, expanding window | 25.43 | 27.45 |
| LightGBM, 180-day window | 27.26 | 27.20 |

Ridge beats the prior-day baseline by 13–14% in both eras, with week-block 95%
intervals clear of zero. LightGBM does not beat ridge, and on native data its
interval against prior-day crosses zero. Native validation is only 91 days.
Inputs are archived versions retrieved in September 2026, not past vintages.

**Uncertainty (M4).** P10/P90 ranges, nominal 80% coverage, on the same validation
days ([report](reports/backtest_quantiles.json)):

| | Hourly coverage | Hourly width | Native coverage | Native width |
|---|---|---|---|---|
| Ridge + past-error interval | 75.5% | 77.0 | 76.6% | 80.4 |
| LightGBM quantiles, raw | 59.8% | 52.6 | 52.0% | 46.4 |
| LightGBM quantiles, conformalized | 80.4% | 87.5 | 82.8% | 88.3 |

Raw quantile models are overconfident; conformal calibration on held-back training
days restores average coverage and gives the lowest P10/P90 pinball loss. Coverage
still varies by time of day: on native data the conformalized interval covers about
70% at midday and over 90% at night, so a single width does not fit every hour. Calibrating the conformal offset per local
hour, a variant designed after seeing these results, narrows the hourly spread of
native coverage from 68–94% to 79–86% at about 4% more width; for ridge it lowers
coverage, because 28 held-back days per hour are too few for the tails. The per-hour
variant needs a fresh forward window before it counts as evidence.

**DE-LU ablation (M4).** Adding lagged German prices (D-1 and D-7 slots, previous-day
mean and AT–DE spread) to the same models and windows changed validation MAE by at
most 0.08 EUR/MWh, with every 95% interval spanning zero
([report](reports/ablation_de_lu.json)). The group is not adopted: AT price lags
already carry what DE-LU adds.

## Sources

- [ENTSO-E Transparency Platform](https://transparency.entsoe.eu): auction prices
  and candidate load/renewable forecasts.
- [Open-Meteo](https://open-meteo.com/en/docs/historical-forecast-api): optional
  weather forecasts, subject to model-run and publication-time verification.

Source references and historical-data limitations are documented in the plan.

## Available commands

Run commands from the project root. Put the token after `ENTSOE_API_KEY=` in `.env`.
Environment variables override `.env` values.

```bash
uv run power-forecast doctor
uv run power-forecast ingest-prices 2026-09-13
uv run --group research pytest -q
uv run ruff check src tests experiments
```

`doctor` checks configuration without contacting ENTSO-E. `ingest-prices` refreshes
one Vienna delivery day. Raw XML and retrieval manifests live in `data/raw/entsoe/`;
validated versions in `data/prices/versions/`; atomic daily checkpoints in
`data/prices/days/`. Earlier `data/normalized/prices/` samples are preserved but are
not used by the versioned pipeline. All downloaded data and forecast artifacts are
ignored by Git.

### Backfill and resume

```bash
# START inclusive, END exclusive. Completed days are checked and skipped.
uv run power-forecast backfill-prices 2026-08-16 2026-09-14
# Re-download explicitly to capture revisions:
uv run power-forecast backfill-prices 2026-09-12 2026-09-14 --refresh
# Full AT history, if desired; this makes one request per uncached day:
uv run power-forecast backfill-prices 2018-10-01 2026-09-14
```

The first implementation uses daily partitions rather than monthly requests to
isolate source failures. It stops on an invalid day and resumes from the same day
on the next invocation. CLI writers share an OS file lock; overlapping jobs fail visibly and can be retried.
An interrupted write cannot advance the completed-day checkpoint. A checksum error
fails visibly; use `--refresh` to repair a cached partition with a new download.

### Forecast and score

```bash
uv run power-forecast forecast 2026-09-13 --replay
# Pass the forecast file path printed by the preceding command:
uv run power-forecast score data/forecasts/RUN_ID.json
# Reproduce the eight-day smoke report:
uv run python experiments/baseline_smoke.py
```

The research reports need the full archive and the `research` dependency group
(numpy, scikit-learn, LightGBM; macOS also needs `brew install libomp`). Production
never installs that group:

```bash
uv run python experiments/coverage_report.py
uv run python experiments/freeze_splits.py
uv run python experiments/backtest_baselines.py
uv run --group research python experiments/backtest_models.py  # about 10 minutes
uv run --group research python experiments/backtest_quantiles.py  # about 20 minutes
```

Without `--replay`, a forecast runs only after its 10:30 Vienna cutoff, using input
versions retrieved by that cutoff. Runs after 10:45 are marked late. Historical data
retrieved today cannot qualify as historical live inputs. `--replay` explicitly
labels reconstructed history and possible revision bias. Forecasts record their
input checksums, code/lock hashes, cutoff, issue time, source slots and fallback use.
The `job publish` command adds an atomic official publication pointer; the standalone
`forecast` command produces diagnostic artifacts only. Scheduler templates are available
and are active on the EC2 host.

The production baseline (`seasonal-daily-v1`) matches the previous day's local clock
slot, prefers the same DST fold, then falls back to the most recent eligible matching slot within 28 days. Hourly
source values used for quarter-hour forecasts are labeled `hourly_derived`; they
are never converted into observed quarter-hour training labels. No eligible history
means no forecast artifact. Uncertainty intervals are not implemented yet.
Official forecasts for delivery before October 16, 2026 used the weekly slot
(`seasonal-weekly-v1`). On the frozen validation windows the prior-day rule had
28% (hourly, 2021–2023) and 18% (native, April–June 2026) lower MAE, with week-block
95% intervals clear of zero; see [the backtest](reports/backtest_baselines.json).
The weekly rule remains the reference for skill.

Scoring pins the current complete actual-price snapshot and retains its checksum;
missing actuals produce a pending diagnostic. A later revision creates a separate
score artifact, leaving the previous result intact. Storage is currently local JSON
with atomic writes; DuckDB analytical storage remains a subsequent milestone task.

The adapter supports A01 and A03 curves, selects SDAC sequence 1 when present,
collapses identical duplicate rows and rejects conflicting overlaps. Negative prices
are valid. See [the source audit](docs/SOURCE_AUDIT.md).

## Daily jobs

```bash
uv run power-forecast job collect
uv run power-forecast job publish
uv run power-forecast job settle
uv run power-forecast job watchdog
```

These default to tomorrow in Vienna. Publication is restricted to 10:30–11:00 on
the preceding day, with immutable cutoff inputs and a 10:45 on-time deadline.
Retries reuse the official forecast. All attempts and latest job outcomes are saved
under `data/operations/`. See [the operations runbook](docs/OPERATIONS.md) for
schedules, container setup, recovery and outstanding deployment acceptance checks.

## Dashboard

```bash
uv run power-forecast dashboard --serve
# Open http://127.0.0.1:8080
```

The dashboard uses the portfolio’s colors, locally hosted fonts and light/dark themes.
It shows forecast/actual curves, day selection, interval details, MAE/RMSE, daily
errors and recorded job status. Historical replays and official forecasts stay
separate. Missing live forecasts and stale exports are explicitly identified.

`dashboard` exports only public fields into `data/dashboard/`; `--serve` starts a
localhost development preview. Re-run the export after new data arrives. Production
uses the `job dashboard` timer and the read-only Nginx service in Compose. See
[dashboard deployment](docs/OPERATIONS.md#dashboard). The historical dashboard is hosted at https://power.kasra.page using S3 and
CloudFront. Daily jobs are active on EC2. The first official forecast is scheduled for
September 17 at 10:30 Vienna, for delivery on September 18, 2026. See the public deployment section in the
runbook for manual updates.
