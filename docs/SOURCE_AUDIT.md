# Initial ENTSO-E source audit

2026-09-14: authenticated A44 request for Austrian delivery day 2026-09-13
succeeded. Credentials were read locally and were not printed. The original response
is retained by SHA-256 under the git-ignored data/raw/entsoe directory.

Observed:

- Area: 10YAT-APG------L, EUR/MWh, PT15M.
- UTC delivery bounds: 2026-09-12T22:00Z to 2026-09-13T22:00Z.
- Two overlapping auction sequences, 1 and 2; selecting sequence 1 produces
  a complete 96-interval day. Do not merge these auction curves.
- Both use A03 variable-size blocks with 95 change points. Expanding each point
  through the next change or period end produces 96 quarter-hours. This is source
  encoding expansion, not interpolation of missing observations.

A03 semantics follow the [ENTSO-E curve specification](https://www.entsoe.eu/Documents/EDI/Library/Introduction_of_different_time_series_v1.4.1.pdf).
Sequence selection follows the SDAC/EXAA distinction documented in the
[entsoe-py project issue](https://github.com/EnergieID/entsoe-py/issues/422).
Independent exchange-price cross-check remains pending. Unclassified legacy series
are accepted only if non-overlapping and otherwise valid; audit them before backfill.

## Backfill and baseline implementation

Additional live checks on 2026-09-14:

| Delivery dates | Result |
|---|---|
| 2026-08-16 through 2026-09-13 | 29 complete days, 2,784 quarter-hours |
| 2025-09-30 | 24 hourly intervals |
| 2025-10-01 | 96 quarter-hours |
| 2025-10-26 | 100 quarter-hours, autumn DST |
| 2026-03-29 | 92 quarter-hours, spring DST |

The August 16 response contained two identical sequence-1 series. The parser now
collapses exact duplicate rows after expansion while rejecting conflicting overlaps.
Raw responses remain intact for inspection. Repeating the recent backfill reported
all 29 days cached, with no download required.

29 offline tests pass, including interrupted-backfill resume, checksum corruption,
future-information rejection, local-clock weekly matching across DST, hourly-derived
fallback, incomplete-history refusal, and pinned/idempotent score results.

The weekly baseline was replayed for September 6–13, 2026: 768 intervals, no fallback
slots, MAE 34.8365 and RMSE 48.7204 EUR/MWh. These are reconstructed historical
inputs downloaded on September 14, not verified historical vintages or a held-out
model comparison. The reproducible entry point is experiments/baseline_smoke.py;
its report is reports/baseline_smoke.json. This smoke run establishes pipeline behavior,
not model superiority.

Still pending: full historical coverage, publication/revision evidence, independent
exchange cross-check, Retry-After-aware throttling, monthly request optimization,
DuckDB persistence, official publication locking and scheduling. Current atomic JSON
checkpoints support one writer; they are not concurrent production transactions.
Retrieval time is recorded and is never substituted for historical publication time.

2026-09-16 operations update: CLI writers now share an OS lock; batch jobs, an
atomic official publication pointer, watchdog and scheduler templates are implemented.
Timers are not active yet. See [the runbook](OPERATIONS.md) for current status.

## M4 covariate eligibility audit (2026-10-15)

Each candidate input is classified by when it is guaranteed to exist relative to the
10:30 Vienna cutoff on D-1 ([design](DESIGN.md#point-in-time-data-rules)). A legal publication deadline is an
upper bound, not proof of when a value first appeared; historical first-seen times
are unknown for everything downloaded today.

| Candidate | Deadline or timing | Class | Use |
|---|---|---|---|
| DE-LU day-ahead prices for D-1 and earlier | Same SDAC auction as the AT target; results published around 13:00 on D-2 | Reconstructed, same evidence as AT prices | Ablated 2026-10-15: no gain, not adopted (reports/ablation_de_lu.json) |
| DE-LU day-ahead prices for D | Set by the auction that forecasts D | Prohibited | Never |
| AT day-ahead total load forecast for D | Art. 6(2)(b): no later than two hours before day-ahead gate closure (12:00), so by 10:00 on D-1 | Eligible by deadline with 30 minutes of margin; historical vintages unverified | Collect prospectively and record first-seen time; ablation only with a reconstructed label |
| AT day-ahead wind and solar forecast for D | Art. 14(2)(d): no later than 18:00 Brussels time on D-1 | Ineligible: the deadline is after the cutoff | Only if prospective collection shows it before 10:30 on every day |
| AT aggregated generation forecast for D | Art. 14(2)(c): no later than 18:00 on D-1 | Ineligible | None |
| Weather, ECMWF IFS HRES 00 UTC run of D-1 (Open-Meteo Single Runs API) | Run is typically available 4–6 hours after initialization, i.e. before the 08:30/09:30 UTC cutoff; publication time is not recorded | Reconstructed, native era only: archived from 2024-03-14, early runs labelled IFS Cycle 49R1 hindcasts, model cycle 50R1 from 2026-05-12 | Ablated 2026-10-17: LightGBM MAE −7.2% (95% interval clear of zero), ridge −3.7% (interval includes zero); production only after forward archiving shows it before cutoff |
| Weather, other models | Single runs archived only from 2026-04-02 | Too short to train | None yet |

The deadlines are quoted from Commission Regulation (EU) No 543/2013, Articles 6(2)(b),
14(2)(c) and 14(2)(d) (consolidated text; Brussels time per Article 2(31)).

ENTSO-E responses carry no publication time (`createdDateTime` is the response time),
so first-seen evidence for the load and wind/solar forecasts comes only from the
production host's 10:00 and 15:00 collections, logged under `observations/` from the
deployment of this change onward. The same collections archive the ECMWF run for D and
log in `observations/weather.jsonl` whether it was already served; the weather group
can enter official runs only once that log shows the run before 10:30 on every day.

Weather caveats: the Historical Forecast API stitches the early hours of successive
runs, so it does not represent one forecast available at a fixed cutoff and is not
used. Single runs before 2024-03-14 do not exist, so the hourly research era
(2018–2025) has no weather at all. The 2026-05-12 cycle change falls inside the native
validation fold. Source: open-meteo.com/en/docs/single-runs-api, read 2026-10-15.

DE-LU prices are stored as a separate archive under `data/zones/DE-LU` with the same
immutable versions and checksums as AT, through `backfill-prices --zone DE-LU`. They
are features only and never enter the target archive.
