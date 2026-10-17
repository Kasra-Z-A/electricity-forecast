import json
from datetime import date
from unittest.mock import patch

from power_forecast.ingestion.entsoe import SourceError
from power_forecast.ingestion.observations import describe, observe

DAY = date(2026, 10, 17)


def document(start="2026-10-16T22:00Z", end="2026-10-17T22:00Z", series=1):
    body = "".join(
        f"""<TimeSeries><curveType>A03</curveType><Period><timeInterval><start>{start}</start>
        <end>{end}</end></timeInterval><resolution>PT15M</resolution>
        <Point><position>1</position><quantity>5</quantity></Point></Period></TimeSeries>"""
        for _ in range(series)
    )
    return f"<GL_MarketDocument>{body}</GL_MarketDocument>".encode()


def test_describe_complete_incomplete_and_missing():
    assert describe(document(series=2), DAY)["status"] == "complete"
    assert describe(document(end="2026-10-17T12:00Z"), DAY)["status"] == "incomplete"
    assert describe(b"<Acknowledgement_MarketDocument/>", DAY)["status"] == "missing"


def test_observe_logs_every_document_even_when_one_fails(tmp_path):
    responses = [document(), SourceError("ENTSO-E request failed (HTTP 503).")]
    with patch("power_forecast.ingestion.observations.fetch_document", side_effect=responses):
        entries = observe(DAY, "test", tmp_path)
    assert [e["status"] for e in entries] == ["complete", "error"]
    logged = [
        json.loads(line)
        for name in ("load_forecast", "wind_solar_forecast")
        for line in (tmp_path / "observations" / f"{name}.jsonl").read_text().splitlines()
    ]
    assert [e["status"] for e in logged] == ["complete", "error"]
    assert "test" not in json.dumps(logged)  # the token never reaches the log
