"""Score an immutable forecast against a pinned complete auction-price snapshot."""

import hashlib
import json
import math
from datetime import date
from pathlib import Path

from power_forecast.history import atomic_json, load_version
from power_forecast.ingestion.entsoe import SourceError


def score(run_path: Path, data_dir: Path) -> Path:
    run = json.loads(run_path.read_text())
    body = {k: v for k, v in run.items() if k != "run_id"}
    if hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != run["run_id"]:
        raise SourceError("Forecast checksum mismatch.")
    day = date.fromisoformat(run["delivery_date"])
    checkpoint = data_dir / "prices" / "days" / f"{day}.json"
    if not checkpoint.exists():
        raise SourceError("Actual prices pending: ingest the complete delivery day first.")
    record = json.loads(checkpoint.read_text())
    _, actuals = load_version(data_dir, record)
    predictions = run["predictions"]
    if [p["start"] for p in predictions] != [a.start.isoformat() for a in actuals]:
        raise SourceError("Forecast and actual interval grids do not match.")
    errors = [p["point"] - a.eur_mwh for p, a in zip(predictions, actuals)]
    result = {
        "metric_version": 1,
        "forecast_run_id": run["run_id"],
        "mode": run["mode"],
        "delivery_date": str(day),
        "actual_snapshot": record,
        "intervals": len(errors),
        "mae": sum(abs(e) for e in errors) / len(errors),
        "rmse": math.sqrt(sum(e * e for e in errors) / len(errors)),
        "unit": "EUR/MWh",
        "fallback_intervals": sum(p["fallback"] for p in predictions),
    }
    digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    path = data_dir / "scores" / f"{digest}.json"
    atomic_json(path, result)
    return path
