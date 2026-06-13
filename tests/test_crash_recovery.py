"""
Crash-recovery breadcrumbs.

A file that KILLS the process mid-embed (OOM / memory recycle) is never caught
by process_file's try/except, so it can never be quarantined the normal way —
it re-attempts on every restart and the indexer loops forever (the 52-hour
production incident). The breadcrumb mechanism fixes that: a marker is written
to disk BEFORE the heavy work, and _recover_inflight() on the next startup
turns any leftover marker into a quarantine strike.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import ingestion.runner as r


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    # Redirect all on-disk state into tmp so tests never touch /var/pkp, and
    # force a cold quarantine cache so each test starts empty.
    monkeypatch.setattr(r, "INFLIGHT_DIR", tmp_path / "inflight")
    monkeypatch.setattr(r, "QUARANTINE_FILE", tmp_path / "bad_files.json")
    monkeypatch.setattr(r, "_quarantine_cache", None)
    return tmp_path


def _write_breadcrumb(tmp_path, tag, oid, name, chash="h1"):
    d = tmp_path / "inflight"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tag}.json").write_text(json.dumps({
        "onedrive_item_id": oid, "file_name": name, "content_hash": chash,
    }))


def test_breadcrumb_start_writes_marker(fresh_state):
    r._breadcrumb_start("walker-0", "id1", "big.csv", "hashX")
    p = r.INFLIGHT_DIR / "walker-0.json"
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["onedrive_item_id"] == "id1"
    assert data["content_hash"] == "hashX"


def test_breadcrumb_clear_removes_marker(fresh_state):
    r._breadcrumb_start("walker-0", "id1", "big.csv", "hashX")
    r._breadcrumb_clear("walker-0")
    assert not (r.INFLIGHT_DIR / "walker-0.json").exists()


def test_clear_is_safe_when_missing(fresh_state):
    # Clearing a non-existent breadcrumb must not raise.
    r._breadcrumb_clear("walker-does-not-exist")


def test_recover_strikes_inflight_file(fresh_state):
    _write_breadcrumb(fresh_state, "walker-1", "poison-id", "poison.csv")
    r._recover_inflight()
    led = r._load_quarantine()
    assert "poison-id" in led
    assert led["poison-id"]["fail_count"] == 1
    # breadcrumb consumed
    assert not (r.INFLIGHT_DIR / "walker-1.json").exists()


def test_three_kills_quarantine_the_killer(fresh_state):
    # The same poison file in-flight across QUARANTINE_THRESHOLD kills must end
    # up quarantined (skipped), breaking the infinite loop.
    for _ in range(r.QUARANTINE_THRESHOLD):
        _write_breadcrumb(fresh_state, "walker-0", "poison-id", "poison.csv", chash="hABC")
        r._recover_inflight()
    skip, entry = r._is_quarantined("poison-id", "hABC")
    assert skip is True
    assert entry["fail_count"] >= r.QUARANTINE_THRESHOLD


def test_innocent_strike_resets_on_success(fresh_state):
    # A file that was merely in-flight once (not the real killer) gets a strike,
    # but a later clean run clears it — so it never drifts toward quarantine.
    _write_breadcrumb(fresh_state, "walker-0", "innocent-id", "doc.pdf")
    r._recover_inflight()
    assert r._load_quarantine()["innocent-id"]["fail_count"] == 1
    r._clear_failure("innocent-id")
    skip, _ = r._is_quarantined("innocent-id", None)
    assert skip is False
    assert "innocent-id" not in r._load_quarantine()


def test_multiple_workers_recovered_together(fresh_state):
    _write_breadcrumb(fresh_state, "walker-0", "id-a", "a.csv")
    _write_breadcrumb(fresh_state, "walker-1", "id-b", "b.csv")
    r._recover_inflight()
    led = r._load_quarantine()
    assert led["id-a"]["fail_count"] == 1
    assert led["id-b"]["fail_count"] == 1
    assert list(r.INFLIGHT_DIR.glob("*.json")) == []


def test_no_inflight_dir_is_noop(fresh_state):
    r._recover_inflight()           # dir doesn't exist yet
    assert r._load_quarantine() == {}


def test_corrupt_breadcrumb_ignored_and_cleaned(fresh_state):
    d = fresh_state / "inflight"
    d.mkdir(parents=True, exist_ok=True)
    (d / "walker-9.json").write_text("{ this is not valid json")
    r._recover_inflight()           # must not raise
    assert not (d / "walker-9.json").exists()
    assert r._load_quarantine() == {}


def test_breadcrumb_tag_is_filesystem_safe(fresh_state):
    # Whatever the thread is named, the tag must be a safe filename.
    tag = r._breadcrumb_tag()
    assert tag
    assert all(c.isalnum() or c in "-_" for c in tag)
