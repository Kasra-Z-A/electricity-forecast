"""Audit archive coverage before freezing split windows; this selects no model.

Run from the repository root after backfilling:
    uv run python experiments/coverage_report.py
"""

from pathlib import Path

from power_forecast.coverage import coverage
from power_forecast.history import atomic_json


def main() -> None:
    report = coverage(Path("data"), verify=True)
    atomic_json(Path("reports/coverage.json"), report)
    span = report.get("span")
    print(
        f"{report['days']} days"
        + (f" {span['first']}..{span['last']}" if span else "")
        + f", missing {report['missing_days']}"
        + f", interval mismatches {len(report.get('interval_mismatches', []))}"
        + f", checksum failures {len(report.get('checksum_failures', []))}"
    )
    for name, era in report.get("eras", {}).items():
        print(f"  {name}: {era['days']} days at {era['resolution_minutes']} min")
    print("complete" if report.get("complete") else "INCOMPLETE")


if __name__ == "__main__":
    main()
