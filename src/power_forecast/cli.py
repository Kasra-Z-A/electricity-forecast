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
def backfill_prices(start: str, end: str, data_dir: Path = Path("data"), refresh: bool = False):
    """Ingest START inclusive to END exclusive, checkpointing each local day."""
    from datetime import timedelta
    from time import sleep

    from power_forecast.history import ingest_day

    try:
        first, stop = date.fromisoformat(start), date.fromisoformat(end)
        if first < date(2018, 10, 1) or stop <= first:
            raise ValueError("Require 2018-10-01 <= START < END (end is exclusive).")
        key = api_key()
        day = first
        while day < stop:
            with writer_lock(data_dir):
                result = ingest_day(day, key, data_dir, refresh)
            typer.echo(
                f"{day}: {'cached' if result['cached'] else 'downloaded'} ({result['intervals']} intervals)"
            )
            if not result["cached"]:
                sleep(0.5)
            day += timedelta(days=1)
    except (ValueError, SourceError, OSError) as error:
        typer.echo(str(error), err=True)
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
