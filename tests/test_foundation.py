from datetime import date
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from power_forecast.cli import app
from power_forecast.ingestion.entsoe import (
    AREA,
    AREAS,
    SourceError,
    archive,
    fetch_prices,
    parse_prices,
)
from power_forecast.timegrid import VIENNA, day_bounds, delivery_grid, forecast_cutoff


@pytest.mark.parametrize("day,count", [("2026-03-29", 92), ("2025-10-26", 100), ("2026-09-14", 96)])
def test_delivery_grid(day, count):
    grid = delivery_grid(date.fromisoformat(day))
    assert len(grid) == count
    assert len(set(grid)) == count
    assert all(t.astimezone(VIENNA).date().isoformat() == day for t in grid)


def test_repeated_hour_and_cutoff():
    grid = delivery_grid(date(2025, 10, 26))
    repeated = [t.astimezone(VIENNA) for t in grid if t.astimezone(VIENNA).hour == 2]
    assert {t.fold for t in repeated} == {0, 1}
    assert forecast_cutoff(date(2026, 7, 1)).hour == 8
    assert forecast_cutoff(date(2026, 1, 1)).hour == 9


def xml(
    points="<Point><position>1</position><price.amount>-12.5</price.amount></Point>",
    resolution="PT15M",
):
    return f"""<Publication_MarketDocument xmlns="urn:test"><mRID>example</mRID><revisionNumber>1</revisionNumber>
    <TimeSeries><currency_Unit.name>EUR</currency_Unit.name><price_Measure_Unit.name>MWH</price_Measure_Unit.name>
    <in_Domain.mRID>{AREA}</in_Domain.mRID><out_Domain.mRID>{AREA}</out_Domain.mRID><curveType>A01</curveType>
    <Period><timeInterval><start>2026-01-01T00:00Z</start><end>2026-01-01T00:15Z</end></timeInterval>
    <resolution>{resolution}</resolution>{points}</Period></TimeSeries></Publication_MarketDocument>""".encode()


def test_negative_prices_and_provenance():
    row = parse_prices(xml())[0]
    assert row.eur_mwh == -12.5
    assert row.revision == "1"
    assert row.minutes == 15


@pytest.mark.parametrize(
    "payload",
    [
        b"bad xml",
        b"<Acknowledgement_MarketDocument/>",
        xml().replace(b">1</position>", b">2</position>"),
        xml().replace(b"-12.5", b"NaN"),
        xml().replace(b"EUR", b"USD"),
        xml().replace(b"A01", b"A02"),
    ],
)
def test_bad_data_rejected(payload):
    with pytest.raises(SourceError):
        parse_prices(payload)


def test_duplicate_series_rejected():
    payload = xml()
    series = payload[
        payload.index(b"<TimeSeries>") : payload.index(b"</TimeSeries>") + len(b"</TimeSeries>")
    ]
    with pytest.raises(SourceError, match="Overlapping"):
        parse_prices(
            payload.replace(
                b"</Publication_MarketDocument>",
                series.replace(b"-12.5", b"99.0") + b"</Publication_MarketDocument>",
            )
        )


def test_archive_idempotency(tmp_path):
    start, end = day_bounds(date(2026, 1, 1))
    first = archive(xml(), tmp_path, start, end)
    assert archive(xml(), tmp_path, start, end) == first
    assert len(list(tmp_path.glob("*.xml"))) == 1
    assert len((tmp_path / "manifest.jsonl").read_text().splitlines()) == 2


def test_secret_safe_transport_error():
    start, end = day_bounds(date(2026, 1, 1))
    with (
        patch(
            "power_forecast.ingestion.entsoe.httpx.get",
            side_effect=httpx.ConnectError("secret-token"),
        ),
        patch("power_forecast.ingestion.entsoe.time.sleep"),
    ):
        with pytest.raises(SourceError) as caught:
            fetch_prices("secret-token", start, end)
    assert "secret-token" not in str(caught.value)


def test_doctor_missing_token():
    with (
        patch("power_forecast.config.load_dotenv"),
        patch.dict("os.environ", {"ENTSOE_API_KEY": ""}),
    ):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "Set ENTSOE_API_KEY" in result.output


def test_ingest_complete_day(tmp_path):
    start, end = day_bounds(date(2026, 1, 1))
    points = "".join(
        f"<Point><position>{i}</position><price.amount>{i}</price.amount></Point>"
        for i in range(1, 97)
    )
    payload = (
        xml(points)
        .replace(b"2026-01-01T00:00Z", start.isoformat().encode())
        .replace(b"2026-01-01T00:15Z", end.isoformat().encode())
    )
    with (
        patch("power_forecast.cli.api_key", return_value="test"),
        patch("power_forecast.history.fetch_prices", return_value=payload),
    ):
        result = CliRunner().invoke(
            app, ["ingest-prices", "2026-01-01", "--data-dir", str(tmp_path)]
        )
    assert result.exit_code == 0, result.output
    assert "96 intervals" in result.output
    assert len(list((tmp_path / "prices" / "versions").glob("*.json"))) == 1


def test_sparse_blocks_expand_to_period_end():
    payload = xml().replace(b"A01", b"A03").replace(b"00:15Z", b"01:00Z")
    rows = parse_prices(payload)
    assert len(rows) == 4
    assert [r.eur_mwh for r in rows] == [-12.5] * 4


def test_other_auction_excluded():
    payload = xml().replace(
        b"<curveType>",
        b"<classificationSequence_AttributeInstanceComponent.position>2</classificationSequence_AttributeInstanceComponent.position><curveType>",
    )
    with pytest.raises(SourceError, match="No price"):
        parse_prices(payload)


def test_identical_duplicate_series_collapsed():
    payload = xml()
    series = payload[
        payload.index(b"<TimeSeries>") : payload.index(b"</TimeSeries>") + len(b"</TimeSeries>")
    ]
    assert (
        len(
            parse_prices(
                payload.replace(
                    b"</Publication_MarketDocument>", series + b"</Publication_MarketDocument>"
                )
            )
        )
        == 1
    )


def test_zone_mismatch_rejected_in_both_directions():
    with pytest.raises(SourceError, match="bidding zone"):
        parse_prices(xml(), AREAS["DE-LU"])
    german = xml().replace(AREA.encode(), AREAS["DE-LU"].encode())
    assert parse_prices(german, AREAS["DE-LU"])[0].eur_mwh == -12.5
    with pytest.raises(SourceError, match="bidding zone"):
        parse_prices(german)
