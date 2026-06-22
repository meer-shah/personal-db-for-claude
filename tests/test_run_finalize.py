"""
Exit-code discipline + Qdrant resilience + completion marker.

The 36-hour silent stall: a Qdrant blip ended the walk and run_full returned 0,
so systemd (Restart=on-failure) never restarted it. These tests pin the new
contract: exit 0 ONLY on genuine completion or deliberate stop; non-zero (restart)
on any abnormal end; and a transient Qdrant disconnect is retried, not fatal.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import ingestion.runner as r


class _Ev:
    def __init__(self, s):
        self._s = bool(s)

    def is_set(self):
        return self._s


# ── _finalize_run: exit-code contract ────────────────────────────────────────
def test_completed_exits_zero_and_writes_marker(monkeypatch, tmp_path):
    m = tmp_path / "done.json"
    monkeypatch.setattr(r, "FULL_INDEX_COMPLETE_FILE", m)
    r._finalize_run(_Ev(False), _Ev(False), completed=True)   # returns (exit 0)
    assert m.exists()


def test_completion_beats_recycle(monkeypatch, tmp_path):
    m = tmp_path / "done.json"
    monkeypatch.setattr(r, "FULL_INDEX_COMPLETE_FILE", m)
    r._finalize_run(_Ev(True), _Ev(True), completed=True)      # completion wins -> exit 0
    assert m.exists()


def test_recycle_exits_75():
    with pytest.raises(SystemExit) as ei:
        r._finalize_run(_Ev(True), _Ev(True), completed=False)
    assert ei.value.code == 75


def test_graceful_stop_exits_zero():
    r._finalize_run(_Ev(False), _Ev(True), completed=False)    # deliberate stop -> exit 0


def test_abnormal_incomplete_exits_one():
    # THE regression: an interrupted walk (no recycle, no stop) must NOT exit 0.
    with pytest.raises(SystemExit) as ei:
        r._finalize_run(_Ev(False), _Ev(False), completed=False)
    assert ei.value.code == 1


# ── _ResilientQdrant: reconnect-and-retry ────────────────────────────────────
def test_retries_conn_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(r.time, "sleep", lambda *_a, **_k: None)
    state = {"n": 0}

    class Fake:
        def retrieve(self, **kw):
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("Server disconnected without sending a response")
            return ["ok"]

    rq = r._ResilientQdrant(lambda: Fake(), attempts=5, base_delay=0)
    assert rq.retrieve(collection_name="x", ids=[1]) == ["ok"]
    assert state["n"] == 3


def test_non_conn_error_raises_immediately(monkeypatch):
    monkeypatch.setattr(r.time, "sleep", lambda *_a, **_k: None)
    state = {"n": 0}

    class Fake:
        def upsert(self, **kw):
            state["n"] += 1
            raise ValueError("bad request")     # a real error, not a disconnect

    rq = r._ResilientQdrant(lambda: Fake(), attempts=5, base_delay=0)
    with pytest.raises(ValueError):
        rq.upsert(collection_name="x", points=[])
    assert state["n"] == 1                       # no retry on a real error


def test_conn_error_exhausts_then_raises(monkeypatch):
    monkeypatch.setattr(r.time, "sleep", lambda *_a, **_k: None)

    class Fake:
        def delete(self, **kw):
            raise ConnectionError("connection reset by peer")

    rq = r._ResilientQdrant(lambda: Fake(), attempts=3, base_delay=0)
    with pytest.raises(ConnectionError):
        rq.delete(collection_name="x")


def test_passthrough_non_retry_attrs():
    class Fake:
        answer = 42

        def close(self):
            return "closed"

    rq = r._ResilientQdrant(lambda: Fake(), attempts=2, base_delay=0)
    assert rq.answer == 42
    assert rq.close() == "closed"


def test_is_qdrant_conn_error_classification():
    assert r._is_qdrant_conn_error(RuntimeError("Server disconnected without sending a response"))
    assert r._is_qdrant_conn_error(ConnectionError("connection reset"))
    assert not r._is_qdrant_conn_error(ValueError("bad payload"))


# ── run_full short-circuits when the completion marker is present ─────────────
def test_run_full_short_circuits_on_marker(monkeypatch, tmp_path):
    m = tmp_path / "complete.json"
    m.write_text("{}")
    monkeypatch.setattr(r, "FULL_INDEX_COMPLETE_FILE", m)
    # Must return immediately (no OneDrive auth, no Qdrant) because the index is
    # already complete -> systemd leaves the service stopped.
    r.run_full(force=False)
