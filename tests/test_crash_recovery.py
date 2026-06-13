"""
Crash-recovery breadcrumbs + watchdog attribution.

A file that KILLS the process mid-process (OOM / memory recycle, or the
no-progress watchdog's deliberate os._exit) is never caught by process_file's
try/except, so it can't be quarantined the normal way. The breadcrumb +
_recover_inflight machinery turns a leftover marker into a quarantine strike.

v2 adds the watchdog sidecar: a deliberate no-progress kill names the culprit
so _recover_inflight quarantines EXACTLY that file (immediate) and spares the
other in-flight files (which were innocent, killed by the hard exit).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import ingestion.runner as r


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    inflight = tmp_path / "inflight"
    monkeypatch.setattr(r, "INFLIGHT_DIR", inflight)
    monkeypatch.setattr(r, "WATCHDOG_TRIP_FILE", inflight / "_watchdog_tripped.json")
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


def test_breadcrumb_touch_refreshes_mtime(fresh_state):
    r._breadcrumb_start("walker-0", "id1", "big.csv", "hashX")
    p = r.INFLIGHT_DIR / "walker-0.json"
    old = p.stat().st_mtime
    os.utime(p, (old - 100, old - 100))   # backdate
    r._breadcrumb_touch("walker-0")
    assert p.stat().st_mtime > old - 100   # touched forward


def test_clear_is_safe_when_missing(fresh_state):
    r._breadcrumb_clear("walker-does-not-exist")


def test_recover_strikes_inflight_file(fresh_state):
    _write_breadcrumb(fresh_state, "walker-1", "poison-id", "poison.csv")
    r._recover_inflight()
    led = r._load_quarantine()
    assert led["poison-id"]["fail_count"] == 1
    assert not (r.INFLIGHT_DIR / "walker-1.json").exists()


def test_three_kills_quarantine_the_killer(fresh_state):
    for _ in range(r.QUARANTINE_THRESHOLD):
        _write_breadcrumb(fresh_state, "walker-0", "poison-id", "poison.csv", chash="hABC")
        r._recover_inflight()
    skip, entry = r._is_quarantined("poison-id", "hABC")
    assert skip is True
    assert entry["fail_count"] >= r.QUARANTINE_THRESHOLD


def test_watchdog_sidecar_quarantines_only_culprit(fresh_state):
    # Watchdog tripped on 'poison' while 'innocent' was also in flight; the hard
    # os._exit killed both. _recover_inflight must quarantine ONLY poison
    # (immediately) and leave innocent un-struck.
    d = fresh_state / "inflight"
    d.mkdir(parents=True, exist_ok=True)
    (d / "_watchdog_tripped.json").write_text(json.dumps({
        "onedrive_item_id": "poison-id", "file_name": "big.csv", "content_hash": "h",
    }))
    _write_breadcrumb(fresh_state, "walker-3", "innocent-id", "ok.pdf", chash="h2")
    r._recover_inflight()
    led = r._load_quarantine()
    assert led["poison-id"]["fail_count"] >= r.QUARANTINE_THRESHOLD   # quarantined NOW
    assert "innocent-id" not in led                                   # innocent spared
    # everything cleared, including the sidecar
    assert list(d.glob("*.json")) == []


def test_innocent_strike_resets_on_success(fresh_state):
    _write_breadcrumb(fresh_state, "walker-0", "innocent-id", "doc.pdf")
    r._recover_inflight()
    assert r._load_quarantine()["innocent-id"]["fail_count"] == 1
    r._clear_failure("innocent-id")
    skip, _ = r._is_quarantined("innocent-id", None)
    assert skip is False
    assert "innocent-id" not in r._load_quarantine()


def test_no_inflight_dir_is_noop(fresh_state):
    r._recover_inflight()
    assert r._load_quarantine() == {}


def test_corrupt_breadcrumb_ignored_and_cleaned(fresh_state):
    d = fresh_state / "inflight"
    d.mkdir(parents=True, exist_ok=True)
    (d / "walker-9.json").write_text("{ this is not valid json")
    r._recover_inflight()
    assert not (d / "walker-9.json").exists()
    assert r._load_quarantine() == {}


def test_breadcrumb_tag_is_filesystem_safe(fresh_state):
    tag = r._breadcrumb_tag()
    assert tag
    assert all(c.isalnum() or c in "-_" for c in tag)
