from datetime import date, timedelta

import pytest
from test_backtest import seed_days

from power_forecast.backtest import load_archive
from power_forecast.baseline import slot_lookup
from power_forecast.features import day_features, feature_names
from power_forecast.ingestion.entsoe import AREA, SourceError
from power_forecast.ingestion.load import parse_load
from power_forecast.timegrid import day_bounds

DAY = date(2026, 9, 17)


def document(day, points, area=AREA, curve="A03", resolution="PT15M"):
    start, end = (t.strftime("%Y-%m-%dT%H:%MZ") for t in day_bounds(day))
    body = "".join(
        f"<Point><position>{p}</position><quantity>{q}</quantity></Point>" for p, q in points
    )
    return f"""<GL_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0">
<TimeSeries><outBiddingZone_Domain.mRID>{area}</outBiddingZone_Domain.mRID>
<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name><curveType>{curve}</curveType>
<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>
<resolution>{resolution}</resolution>{body}</Period></TimeSeries></GL_MarketDocument>""".encode()


def test_a03_points_fill_forward_and_dst_days_keep_their_length():
    values = parse_load(document(DAY, [(1, 5000), (3, 5200), (96, 6000)]), DAY)
    assert len(values) == 96
    assert values[:4] == [5000, 5000, 5200, 5200] and values[-1] == 6000
    for day, count in ((date(2026, 3, 29), 92), (date(2025, 10, 26), 100)):
        assert len(parse_load(document(day, [(1, 1)]), day)) == count


def test_incomplete_or_foreign_documents_are_rejected():
    with pytest.raises(SourceError, match="positions"):
        parse_load(document(DAY, [(2, 5000)]), DAY)
    with pytest.raises(SourceError, match="positions"):
        parse_load(document(DAY, [(1, 5000)], curve="A01"), DAY)
    with pytest.raises(SourceError, match="zone"):
        parse_load(document(DAY, [(1, 5000)], area="10Y1001A1001A82H"), DAY)
    with pytest.raises(SourceError, match="resolution"):
        parse_load(document(DAY, [(1, 5000)], resolution="PT60M"), DAY)
    with pytest.raises(SourceError, match="cover"):
        parse_load(document(DAY - timedelta(days=1), [(1, 5000)]), DAY)


def test_load_features_follow_the_delivery_grid(tmp_path):
    seed_days(tmp_path, DAY - timedelta(days=21), DAY - timedelta(days=1))
    archive = load_archive(tmp_path)
    lookup = slot_lookup(archive.values())
    names = feature_names(("load",))
    forecast = {DAY: {"mw": [float(n) for n in range(96)]}}
    rows = day_features(archive, lookup, DAY, {"load": forecast})
    assert len(rows) == 96 and all(len(row) == len(names) for row in rows)
    assert rows[5][names.index("load_forecast")] == 5
    assert {row[names.index("load_forecast_day")] for row in rows} == {47.5}
    assert day_features(archive, lookup, DAY, {"load": {}}) is None


def test_one_request_fills_each_day_and_leaves_a_defective_day_missing(tmp_path, monkeypatch):
    from power_forecast.ingestion import load

    first, last = DAY, DAY + timedelta(days=2)
    # The period covers only the first two days; the third is missing from the response.
    payload = document(first, [(1, 5000)]).replace(
        f"<end>{day_bounds(first)[1].strftime('%Y-%m-%dT%H:%MZ')}</end>".encode(),
        f"<end>{day_bounds(first + timedelta(days=1))[1].strftime('%Y-%m-%dT%H:%MZ')}</end>".encode(),
    )
    calls = []
    monkeypatch.setattr(load, "fetch_document", lambda *args: calls.append(args) or payload)
    results = load.ingest_load_days(first, last, "key", tmp_path)
    assert len(calls) == 1 and calls[0][2:] == (day_bounds(first)[0], day_bounds(last)[1])
    assert not results[first]["cached"] and not results[first + timedelta(days=1)]["cached"]
    assert isinstance(results[last], SourceError)
    assert set(load.load_load(tmp_path)) == {first, first + timedelta(days=1)}
    again = load.ingest_load_days(first, first + timedelta(days=1), "key", tmp_path)
    assert len(calls) == 1 and all(record["cached"] for record in again.values())
