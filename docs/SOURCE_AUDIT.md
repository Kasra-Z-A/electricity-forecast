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
