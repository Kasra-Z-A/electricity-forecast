"""Chronos-2 zero-shot forecasts; needs the `foundation` dependency group.

No weights are trained here. For delivery day D the model reads the native price
series up to the end of D's last label day, together with the load forecast and
weather run each of those days had, and the load forecast and weather run for D
itself as known future inputs. Hourly weather repeats over its four quarter-hours.
The context stops at the newest gap in the price archive, since the model needs a
regular series. P50 is the point forecast; P10 and P90 are reported alongside.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from power_forecast.features import WEATHER_VARIABLES, _weather_hours
from power_forecast.history import resolution
from power_forecast.splits import last_label_day
from power_forecast.timegrid import VIENNA, delivery_grid

MODEL = "amazon/chronos-2"
# Pinned weights, downloaded once and checked against the published SHA-256.
REVISION = "29ec3766d36d6f73f0696f85560a422f50e8498c"
WEIGHTS_SHA256 = "ddcda3c7508bf2528087723e98a20707cc04b7f370ae275a9fd88078ddba4f42"
CONTEXT_DAYS = 84
QUANTILES = [0.1, 0.5, 0.9]
COVARIATES = ("load", *WEATHER_VARIABLES)


def _covariates(day: date, load: dict, weather: dict) -> list[list[float]] | None:
    """[load, temperature, wind, solar] per interval of `day`, or None if missing."""
    if day not in load or day not in weather:
        return None
    hours = _weather_hours(weather[day], day)
    rows = []
    for moment, mw in zip(delivery_grid(day), load[day]["mw"], strict=True):
        values = hours.get(moment.replace(minute=0))
        if values is None:
            return None
        rows.append([mw, *values])
    return rows


def frames(archive: dict, load: dict, weather: dict, day: date):
    """(context, future) frames for delivery `day`, or None without eligible inputs."""
    future = _covariates(day, load, weather)
    if future is None:
        return None
    newest = last_label_day(day)
    history = []
    current = newest
    while len(history) < CONTEXT_DAYS and current in archive and resolution(current) == 15:
        covariates = _covariates(current, load, weather)
        if covariates is None:
            break
        prices = [row.eur_mwh for row in archive[current][1]]
        history.append((current, prices, covariates))
        current -= timedelta(days=1)
    if len(history) < 7:
        return None
    rows = []
    for current, prices, covariates in reversed(history):
        for moment, price, values in zip(delivery_grid(current), prices, covariates, strict=True):
            rows.append([str(day), moment.replace(tzinfo=None), price, *values])
    context = pd.DataFrame(rows, columns=["item_id", "timestamp", "target", *COVARIATES])
    future_rows = [
        [str(day), moment.replace(tzinfo=None), *values]
        for moment, values in zip(delivery_grid(day), future, strict=True)
    ]
    return context, pd.DataFrame(future_rows, columns=["item_id", "timestamp", *COVARIATES])


def model_dir(data_dir: Path) -> Path:
    return data_dir / "models" / f"chronos-2-{REVISION}"


def load_pipeline(data_dir: Path):
    from chronos import Chronos2Pipeline

    return Chronos2Pipeline.from_pretrained(str(model_dir(data_dir)), device_map="cpu")


def forecast_days(pipeline, archive: dict, load: dict, weather: dict, days: list[date]) -> dict:
    """{day: (n, 3) array of P10/P50/P90 on the day's UTC grid} for days with inputs."""
    pairs = {day: pair for day in days if (pair := frames(archive, load, weather, day))}
    forecasts = {}
    # Every day of a batch must ask for the same horizon, so DST days go separately.
    lengths = sorted({len(pair[1]) for pair in pairs.values()})
    for length in lengths:
        batch = [day for day, pair in pairs.items() if len(pair[1]) == length]
        result = pipeline.predict_df(
            pd.concat([pairs[day][0] for day in batch], ignore_index=True),
            future_df=pd.concat([pairs[day][1] for day in batch], ignore_index=True),
            prediction_length=length,
            quantile_levels=QUANTILES,
            freq="15min",
        )
        for day in batch:
            rows = result[result["item_id"] == str(day)].sort_values("timestamp")
            expected = [moment.replace(tzinfo=None) for moment in delivery_grid(day)]
            if list(pd.to_datetime(rows["timestamp"])) != expected:
                raise ValueError(f"Chronos-2 returned an unexpected grid for {day}.")
            forecasts[day] = rows[[str(q) for q in QUANTILES]].to_numpy(dtype=float)
    return forecasts


def forecast_isolated(data_dir: Path, days: list[date]) -> dict:
    """forecast_days in a fresh interpreter, returning the same mapping.

    PyTorch and LightGBM each bring their own OpenMP runtime on macOS; with both
    active in one process their worker threads deadlock. The child never imports
    LightGBM, so a process that has already trained LightGBM can still use Chronos-2.
    """
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "forecasts.json"
        subprocess.run(
            [sys.executable, "-m", "power_forecast.chronos", str(data_dir), str(output)]
            + [str(day) for day in days],
            check=True,
        )
        stored = json.loads(output.read_text())
    return {date.fromisoformat(day): np.array(values) for day, values in stored.items()}


def _main(arguments: list[str]) -> None:
    from power_forecast.backtest import load_archive
    from power_forecast.ingestion.load import load_load
    from power_forecast.ingestion.weather import load_weather

    data_dir, output = Path(arguments[0]), Path(arguments[1])
    days = [date.fromisoformat(value) for value in arguments[2:]]
    forecasts = forecast_days(
        load_pipeline(data_dir),
        load_archive(data_dir),
        load_load(data_dir),
        load_weather(data_dir),
        days,
    )
    output.write_text(json.dumps({str(day): value.tolist() for day, value in forecasts.items()}))


def results(archive: dict, forecasts: dict, days: list[date]) -> list[dict]:
    """backtest.py format, with P10/P50/P90 per interval; `error` is P50 minus actual."""
    output = []
    for day in days:
        if day not in forecasts or day not in archive:
            output.append({"day": day, "intervals": None})
            continue
        quantiles = np.sort(forecasts[day], axis=1)
        output.append(
            {
                "day": day,
                "intervals": [
                    {
                        "local": actual.start.astimezone(VIENNA),
                        "actual": actual.eur_mwh,
                        "error": float(q[1]) - actual.eur_mwh,
                        "fallback": False,
                        "quantiles": [float(v) for v in q],
                        "crossed": bool(np.any(np.diff(raw) < 0)),
                    }
                    for q, raw, actual in zip(
                        quantiles, forecasts[day], archive[day][1], strict=True
                    )
                ],
            }
        )
    return output


if __name__ == "__main__":
    _main(sys.argv[1:])
