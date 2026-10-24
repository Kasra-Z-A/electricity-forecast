# Austrian Day-Ahead Electricity Price Forecasting

Every morning this system forecasts the next day's Austrian day-ahead electricity
prices before the auction, keeps exactly what it knew when it did so, and scores
each forecast once the auction result is published. It is built as a production
ML system: point-in-time inputs, rolling backtests on frozen windows, scheduled
batch jobs, a shadow candidate with an automatic promotion gate, and rollback.

Live dashboard: https://power.kasra.page

- **Target:** native 15-minute Austrian SDAC prices; older hourly history is a
  separate research track.
- **Timing:** inputs freeze at 10:30 Europe/Vienna on the day before delivery;
  publication is due by 10:45.
- **Production model:** the prior-day seasonal baseline. A ridge model runs in
  shadow beside it and is promoted automatically once it passes the gate.
- **Infrastructure:** batch jobs in one AWS Lambda function started by EventBridge
  Scheduler, state as a versioned S3 snapshot, an image built in CI and deployed by
  digest, a static dashboard on S3 + CloudFront.

How it is built and why: [docs/DESIGN.md](docs/DESIGN.md). Running and recovering
it: [docs/OPERATIONS.md](docs/OPERATIONS.md). Source checks:
[docs/SOURCE_AUDIT.md](docs/SOURCE_AUDIT.md).

## Status

| Part | State |
|---|---|
| Price archive, baselines, scoring | Running |
| Scheduled daily operation and dashboard | Running: 28 consecutive on-time scheduled forecasts by 2026-10-14 ([report](reports/m2_observation_2026-10-14.json)) |
| Frozen splits, backtests of baselines, ridge and LightGBM | Done |
| Covariate audit, ablations, calibrated P10/P50/P90 | Backtested; not in production |
| Shadow candidate, automatic promotion, rollback | Running: shadow, gate and dashboard comparison are live |
| Comparison of LEAR, LightGBM, Chronos-2 and an ensemble on weather and load forecasts | Done; not in production |

## Results

**Validation backtest.** Weekly-refit rolling origins on frozen windows, MAE in
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

**Holdout.** The candidate `ridge-core-180-v1` ([config](configs/candidate.json)) was
frozen before the native holdout (July–August 2026) was consulted, once: MAE 21.35
against 25.70 for prior-day, 17% skill, with a week-block interval clear of zero
([report](reports/holdout_native.json)). The holdout is now spent; further evidence
comes from shadow operation.

**Uncertainty.** P10/P90 ranges, nominal 80% coverage, on the same validation
days ([report](reports/backtest_quantiles.json)):

| | Hourly coverage | Hourly width | Native coverage | Native width |
|---|---|---|---|---|
| Ridge + past-error interval | 75.5% | 77.0 | 76.6% | 80.4 |
| LightGBM quantiles, raw | 59.8% | 52.6 | 52.0% | 46.4 |
| LightGBM quantiles, conformalized | 80.4% | 87.5 | 82.8% | 88.3 |

Raw quantile models are overconfident; conformal calibration on held-back training
days restores average coverage and gives the lowest P10/P90 pinball loss. Coverage
still varies by time of day: on native data the conformalized interval covers about
70% at midday and over 90% at night, so a single width does not fit every hour.
Calibrating the conformal offset per local hour, a variant designed after seeing
these results, narrows the hourly spread of native coverage from 68–94% to 79–86% at
about 4% more width; for ridge it lowers coverage, because 28 held-back days per
hour are too few for the tails. Since the variant was designed on this data, its
numbers are not independent evidence.

**DE-LU ablation.** Adding lagged German prices (D-1 and D-7 slots, previous-day
mean and AT–DE spread) to the same models and windows changed validation MAE by at
most 0.08 EUR/MWh, with every 95% interval spanning zero
([report](reports/ablation_de_lu.json)). The group is not adopted: AT price lags
already carry what DE-LU adds.

**Weather ablation.** Six-city means of ECMWF IFS temperature, 100 m wind and
solar radiation from the 00 UTC run of D-1 (Open-Meteo single runs, data CC BY 4.0
Open-Meteo.com), native era only ([report](reports/ablation_weather.json)):

| Native Apr–Jun 2026 | Without weather | With weather | 95% interval, daily MAE difference |
|---|---|---|---|
| Ridge | 26.22 | 25.26 | [−1.96, +0.15] |
| LightGBM | 26.80 | **24.88** | [−2.90, −0.91] |

Weather helps most at midday and on negative-price intervals, and makes LightGBM the
best model. The archived runs are reconstructed (early ones are recomputed
hindcasts), ECMWF changed model cycle on 2026-05-12 inside the fold, and one day with
a defective run (2026-06-24) is excluded from both sides. Production forecasts do
not use weather.

**Model comparison.** Four models on the day-ahead load forecast and the weather
run, with settings fixed before any was scored ([config](configs/comparison.json)):

- LEAR: one Lasso per quarter-hour slot on the whole price curves of D-1, D-2, D-3
  and D-7, the load forecast for D, D-1 and D-7 and the weather run for D,
  averaged over 56-, 84- and 182-day training windows;
- LightGBM with the weather and load features;
- Chronos-2, a pretrained time-series model used zero-shot, with load and weather
  as inputs;
- the equal-weight mean of those three.

All train on native quarter-hour data only and refit weekly. The model with the
lowest MAE on the selection window was chosen, and the confirmation window was then
scored once ([selection](reports/comparison_selection.json),
[confirmation](reports/comparison_confirmation.json)):

| MAE, EUR/MWh | Selection Apr–Aug 2026, 145 days | Confirmation Sep–Oct 2026, 50 days |
|---|---|---|
| Weekly baseline | 33.05 | 35.80 |
| Prior-day baseline (production) | 28.32 | 36.11 |
| Ridge candidate | 24.28 | 30.33 |
| LightGBM, weather and load | 22.29 | 26.26 |
| LEAR | 21.33 | 26.78 |
| Ensemble | 19.12 | 23.20 |
| Chronos-2, zero-shot | **18.78** | **21.85** |

Chronos-2 is selected and confirmed: on the confirmation window its MAE is 28% below
the ridge candidate, with a week-block 95% interval of the daily difference of
[−10.0, −6.7] EUR/MWh. It is also the most accurate on high-price intervals. Its
P10–P90 range covers 80.4% of intervals overall but 51% in its worst hour. The
ensemble is less accurate than Chronos-2 alone.

The selection window contains the native holdout consulted for the ridge candidate;
no setting was chosen on it. Weather and load inputs are archived versions; their
published schedules (00 UTC run of D-1, load forecast due 10:00 on D-1) are the
availability evidence. Chronos-2 was released before both windows, so they cannot be
part of its training data. No winter days are evaluated. Chronos-2 needs an unbroken
input history, so the defective weather run of 2026-06-24 leaves it without a
forecast for the following week; those days are excluded for every model.

## Local setup

Python 3.12 and [uv](https://docs.astral.sh/uv/). Dependencies are pinned in `uv.lock`.

```bash
uv sync
cp -n .env.example .env   # then set ENTSOE_API_KEY; never commit it
uv run power-forecast doctor        # checks configuration without contacting ENTSO-E
uv run --group research pytest -q
uv run ruff check src tests experiments
```

Environment variables override `.env` values. Downloaded data and forecast artifacts
live under `data/` and are ignored by Git.

### Archive prices

```bash
uv run power-forecast ingest-prices 2026-09-13          # one Vienna delivery day
# START inclusive, END exclusive. Completed days are checked and skipped.
uv run power-forecast backfill-prices 2026-08-16 2026-09-14
uv run power-forecast backfill-prices 2026-09-12 2026-09-14 --refresh   # capture revisions
uv run power-forecast backfill-prices 2018-10-01 2026-09-14             # full AT history
```

Raw XML and retrieval manifests go to `data/raw/entsoe/`, validated versions to
`data/prices/versions/` and atomic daily checkpoints to `data/prices/days/`. Backfill
stops on an invalid day and resumes from it on the next run; an interrupted write
cannot advance the checkpoint, and a checksum error is repaired with `--refresh`.
The adapter supports A01 and A03 curves, selects SDAC sequence 1 when present,
collapses identical duplicate rows and rejects conflicting overlaps. Negative prices
are valid.

### Forecast and score

```bash
uv run power-forecast forecast 2026-09-13 --replay
uv run power-forecast score data/forecasts/RUN_ID.json   # path printed by forecast
```

Without `--replay`, a forecast runs only after its 10:30 cutoff, using input versions
retrieved by then; `--replay` labels reconstructed history and possible revision
bias. Forecasts record their input checksums, code and lock hashes, cutoff, issue
time, source slots and fallback use. The standalone `forecast` command writes
diagnostic artifacts only; `job publish` writes the official forecast. Scoring pins
the complete actual-price snapshot it used, and a later revision creates a separate
score.

### Research reports

These need the full archive and the `research` dependency group (numpy,
scikit-learn, LightGBM; macOS also needs `brew install libomp`). Production never
installs that group.

```bash
uv run python experiments/coverage_report.py
uv run python experiments/freeze_splits.py
uv run python experiments/backtest_baselines.py
uv run --group research python experiments/backtest_models.py     # about 10 minutes
uv run --group research python experiments/backtest_quantiles.py  # about 20 minutes
uv run power-forecast backfill-weather 2025-10-01 2026-10-18
uv run --group research python experiments/ablation.py weather
```

The model comparison also needs the `foundation` group (PyTorch and Chronos-2) and
the Chronos-2 weights at the revision pinned in `chronos.py`, which the script checks
against their published SHA-256:

```bash
REV=29ec3766d36d6f73f0696f85560a422f50e8498c
mkdir -p data/models/chronos-2-$REV
for f in config.json model.safetensors; do
  curl -L -C - -o data/models/chronos-2-$REV/$f \
    https://huggingface.co/amazon/chronos-2/resolve/$REV/$f
done
uv run power-forecast backfill-weather 2024-03-15 2026-10-21
uv run power-forecast backfill-load 2018-10-01 2026-10-22
uv run --group research --group foundation python experiments/compare_models.py validation
uv run --group research --group foundation python experiments/compare_models.py holdout
```

The second run scores the confirmation window and refuses to run again once its
report exists.

### Daily jobs

```bash
uv run power-forecast job collect
uv run power-forecast job publish
uv run power-forecast job settle
uv run power-forecast job watchdog
```

They default to tomorrow in Vienna. Publication is allowed from 10:30 to 11:00 on the
day before, with a 10:45 on-time deadline, and retries reuse the official forecast.
Schedules, container setup and recovery are in [the runbook](docs/OPERATIONS.md).

### Dashboard

```bash
uv run power-forecast dashboard --serve   # http://127.0.0.1:8080
```

The dashboard shows forecast and actual curves, day selection, interval details,
MAE/RMSE, daily errors, the model comparison and recorded job status, in the
portfolio site's design with self-hosted fonts and light/dark themes. The export
contains public fields only; replays and shadow forecasts never appear in its
charts.

## Repository layout

```text
src/power_forecast/   package: ingestion, time grid, baselines, ridge, LEAR,
                      Chronos-2, scoring, operations, lifecycle and promotion,
                      dashboard export
experiments/          reproducible research entry points
configs/              frozen splits, model settings and the current candidate
reports/              checksummed results of backtests, ablations and checks
deploy/               Lambda deployment script
docs/                 design, operations runbook, source audit
tests/                offline tests with sanitised fixtures
```

## Data sources

- [ENTSO-E Transparency Platform](https://transparency.entsoe.eu): auction prices
  and load/renewable forecasts.
- [Open-Meteo](https://open-meteo.com): ECMWF IFS single-run weather forecasts,
  CC BY 4.0.
- [Chronos-2](https://huggingface.co/amazon/chronos-2) (Amazon), pretrained weights,
  Apache-2.0.
