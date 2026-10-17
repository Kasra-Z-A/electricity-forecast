"""Initial diagnostic and audited single-day ingestion commands."""

from datetime import date, datetime
from pathlib import Path

import typer

from power_forecast.config import api_key
from power_forecast.ingestion.entsoe import SourceError
from power_forecast.operations import writer_lock
from power_forecast.timegrid import VIENNA

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


@app.command()
def doctor():
    """Check local configuration without printing credentials or making requests."""
    try:
        api_key()
    except ValueError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)
    typer.echo("ENTSO-E token is configured (authentication not yet checked).")
    typer.echo(f"Market timezone: {VIENNA}; current local date: {datetime.now(VIENNA).date()}")


@app.command("ingest-prices")
def ingest_prices(day: str, data_dir: Path = Path("data")):
    """Fetch one local delivery DAY (YYYY-MM-DD), archive XML, validate and normalize."""
    from power_forecast.history import ingest_day

    try:
        with writer_lock(data_dir):
            result = ingest_day(date.fromisoformat(day), api_key(), data_dir, refresh=True)
    except (ValueError, SourceError, OSError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)
    typer.echo(f"Validated {result['intervals']} intervals for {day}.")
    typer.echo(f"Normalized: {data_dir / result['path']}")


@app.command("backfill-prices")
def backfill_prices(
    start: str,
    end: str,
    data_dir: Path = Path("data"),
    refresh: bool = False,
    zone: str = "AT",
    window_days: int = 1,
):
    """Ingest START inclusive to END exclusive, checkpointing each local day.

    Zones other than AT are covariates and default to data/zones/ZONE, never the
    target archive. --window-days N fetches N days per request; a window that fails
    validation is retried one day at a time.
    """
    from datetime import timedelta
    from time import sleep

    from power_forecast.history import ingest_day, ingest_window
    from power_forecast.ingestion.entsoe import AREAS

    if window_days < 1 or (refresh and window_days > 1):
        typer.echo(
            "--window-days must be positive and cannot be combined with --refresh.", err=True
        )
        raise typer.Exit(2)
    if zone not in AREAS:
        typer.echo(f"Unknown zone; choose one of {', '.join(AREAS)}.", err=True)
        raise typer.Exit(2)
    if zone != "AT" and data_dir == Path("data"):
        data_dir = Path("data") / "zones" / zone
    try:
        first, stop = date.fromisoformat(start), date.fromisoformat(end)
        if first < date(2018, 10, 1) or stop <= first:
            raise ValueError("Require 2018-10-01 <= START < END (end is exclusive).")
        key = api_key()
        day = first
        while window_days > 1 and day < stop:
            window = [day + timedelta(days=n) for n in range(window_days)]
            window = [d for d in window if d < stop]
            try:
                with writer_lock(data_dir):
                    stored = ingest_window(window, key, data_dir, AREAS[zone])
                typer.echo(f"{window[0]}..{window[-1]}: downloaded {len(stored)} days")
            except SourceError as error:
                typer.echo(f"{window[0]}..{window[-1]}: {error} Retrying per day.", err=True)
                for single in window:
                    with writer_lock(data_dir):
                        ingest_day(single, key, data_dir, False, AREAS[zone])
                    sleep(0.5)
            sleep(0.5)
            day = window[-1] + timedelta(days=1)
        while day < stop:
            with writer_lock(data_dir):
                result = ingest_day(day, key, data_dir, refresh, AREAS[zone])
            typer.echo(
                f"{day}: {'cached' if result['cached'] else 'downloaded'} ({result['intervals']} intervals)"
            )
            if not result["cached"]:
                sleep(0.5)
            day += timedelta(days=1)
    except (ValueError, SourceError, OSError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)


@app.command("backfill-weather")
def backfill_weather(start: str, end: str, data_dir: Path = Path("data")):
    """Archive the 00 UTC single run of D-1 for delivery days START to END (exclusive)."""
    from datetime import timedelta
    from time import sleep

    from power_forecast.ingestion.weather import ingest_weather_day

    failed = []
    try:
        day, stop = date.fromisoformat(start), date.fromisoformat(end)
        while day < stop:
            try:
                with writer_lock(data_dir):
                    result = ingest_weather_day(day, data_dir)
                typer.echo(
                    f"{day}: {'cached' if result['cached'] else 'downloaded'} ({result['run']})"
                )
            except SourceError as error:
                # A defective archived run is left missing, never filled from another run.
                failed.append(str(day))
                typer.echo(f"{day}: {error}", err=True)
            sleep(0.5)
            day += timedelta(days=1)
    except (ValueError, OSError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)
    if failed:
        typer.echo(f"Missing days: {', '.join(failed)}", err=True)
        raise typer.Exit(1)


@app.command("forecast")
def forecast_command(
    delivery_date: str,
    data_dir: Path = Path("data"),
    replay: bool = False,
    model: str | None = None,
):
    """Save a seasonal baseline. --replay explicitly permits reconstructed history."""
    from power_forecast.baseline import MODELS, PRODUCTION_MODEL, forecast

    model = model or PRODUCTION_MODEL
    if model not in MODELS:
        typer.echo(f"Unknown model; choose one of {', '.join(MODELS)}.", err=True)
        raise typer.Exit(2)
    try:
        with writer_lock(data_dir):
            output = forecast(date.fromisoformat(delivery_date), data_dir, replay, model=model)
    except (ValueError, SourceError, OSError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)
    typer.echo(f"Forecast saved: {output}")


@app.command("shadow-replay")
def shadow_replay(start: str, end: str, data_dir: Path = Path("data")):
    """Recreate candidate forecasts for START inclusive to END exclusive.

    Each day uses only the price versions retrieved before its cutoff, so run this
    on the archive that collected them live. Training labels may be later downloads
    and are marked reconstructed. Existing shadow days are kept.
    """
    from datetime import timedelta

    from power_forecast.lifecycle import shadow_forecast
    from power_forecast.timegrid import forecast_cutoff

    try:
        day, stop = date.fromisoformat(start), date.fromisoformat(end)
        while day < stop:
            with writer_lock(data_dir):
                record = shadow_forecast(day, data_dir, forecast_cutoff(day), vintage=True)
            typer.echo(f"{day}: {'kept' if record.get('reused') else record['mode']}")
            day += timedelta(days=1)
    except (ValueError, SourceError, OSError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)


@app.command("score")
def score_command(run: Path, data_dir: Path = Path("data")):
    """Score a forecast JSON file against the latest complete actual-price snapshot."""
    import json

    from power_forecast.scoring import score

    try:
        with writer_lock(data_dir):
            path = score(run, data_dir)
        metrics = json.loads(path.read_text())
    except (ValueError, SourceError, OSError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)
    typer.echo(f"MAE: {metrics['mae']:.3f}; RMSE: {metrics['rmse']:.3f} EUR/MWh")
    typer.echo(f"Score saved: {path}")


@app.command("job")
def job_command(job: str, data_dir: Path = Path("data"), delivery_date: str | None = None):
    """Run collect, publish, settle, watchdog or dashboard; delivery defaults to tomorrow."""
    import json

    from power_forecast.operations import run_job

    try:
        result = run_job(
            job, data_dir, date.fromisoformat(delivery_date) if delivery_date else None
        )
    except (ValueError, SourceError, OSError) as error:
        typer.echo(
            f"Job failed ({type(error).__name__}); check configuration and runbook.", err=True
        )
        raise typer.Exit(1)
    typer.echo(json.dumps(result, indent=2))
    if result["status"] != "success":
        raise typer.Exit(1)


@app.command("dashboard")
def dashboard_command(
    data_dir: Path = Path("data"),
    output_dir: Path | None = None,
    serve: bool = False,
    port: int = 8080,
    static_preview: bool = False,
):
    """Export the dashboard; --serve previews it on localhost (development only)."""
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    from power_forecast.dashboard import build_dashboard

    try:
        with writer_lock(data_dir):
            output = build_dashboard(data_dir, output_dir, static_preview=static_preview)
        typer.echo(f"Dashboard exported: {output}")
        if serve:
            handler = partial(SimpleHTTPRequestHandler, directory=str(output.resolve()))
            with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
                typer.echo(f"Preview: http://127.0.0.1:{port}")
                server.serve_forever()
    except (ValueError, SourceError, OSError) as error:
        typer.echo(f"Dashboard failed ({type(error).__name__}).", err=True)
        raise typer.Exit(1)
