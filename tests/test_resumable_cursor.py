import json
from ingestion import runner


def test_extract_delta_token_formats():
    assert runner._extract_delta_token("https://g/delta?token=ABC123") == "ABC123"
    assert runner._extract_delta_token("https://g/delta?token=aW5pdA%3D%3D") == "aW5pdA=="
    assert runner._extract_delta_token("https://g/delta(token='Q99')") == "Q99"
    assert runner._extract_delta_token("https://g/delta?$select=id&token=t_sel") == "t_sel"
    assert runner._extract_delta_token("") is None
    assert runner._extract_delta_token("https://g/delta?nope=1") is None


def test_full_cursor_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "FULL_CURSOR_FILE", tmp_path / "fc.json")
    assert runner._load_full_cursor() is None
    runner._save_full_cursor("https://g/delta?token=p2")
    assert runner._load_full_cursor() == "https://g/delta?token=p2"
    runner._save_full_cursor("https://g/delta?token=p3")   # overwrite
    assert runner._load_full_cursor() == "https://g/delta?token=p3"
    runner._clear_full_cursor()
    assert runner._load_full_cursor() is None
