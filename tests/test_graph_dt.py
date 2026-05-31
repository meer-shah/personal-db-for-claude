from datetime import datetime, timezone

from ingestion.runner import _parse_graph_dt, _same_instant


def test_z_equals_offset():
    assert _same_instant("2023-11-26T09:01:20Z", "2023-11-26T09:01:20+00:00")


def test_seven_digit_fraction_does_not_crash():
    dt = _parse_graph_dt("2023-11-26T09:01:20.1234567Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert _same_instant("2023-11-26T09:01:20.1234567Z",
                         "2023-11-26T09:01:20.123456+00:00")


def test_roundtrip_isoformat_matches_graph():
    raw = "2023-11-26T09:01:20Z"
    stored = _parse_graph_dt(raw).isoformat()
    assert _same_instant(stored, raw)


def test_none_and_garbage():
    assert _parse_graph_dt(None) is None
    assert _parse_graph_dt("") is None
    assert _parse_graph_dt("not-a-date") is None
    assert not _same_instant("not-a-date", "2023-11-26T09:01:20Z")


def test_different_instants_not_equal():
    assert not _same_instant("2023-11-26T09:01:20Z", "2023-11-26T09:01:21Z")
