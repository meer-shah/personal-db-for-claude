"""
Streaming process_file — the v2 fix that indexes arbitrarily large files in
bounded memory by feeding parse -> chunk -> embed -> upsert one batch at a time
and committing only after the last batch.

These tests stub the embedder and Qdrant calls (no model load, no server) and
assert the batch/commit/cleanup contract.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import ingestion.runner as r


def _setup(monkeypatch, tmp_path, n_raw, batch):
    calls = {"delete": 0, "commit": 0, "upserts": [], "indices": [], "touch": 0}
    monkeypatch.setattr(r, "_existing_hash", lambda c, o: None)        # force processing
    monkeypatch.setattr(r, "_is_quarantined", lambda o, h: (False, None))
    monkeypatch.setattr(r, "_clear_failure", lambda o: None)
    monkeypatch.setattr(r, "_delete_item_chunks",
                        lambda c, o: calls.__setitem__("delete", calls["delete"] + 1))

    def fake_upsert(c, chunks, commit=True):
        calls["upserts"].append((len(chunks), commit))
        calls["indices"].extend(ch.chunk_index for ch in chunks)

    monkeypatch.setattr(r, "_upsert_chunks", fake_upsert)
    monkeypatch.setattr(r, "_mark_committed",
                        lambda c, o: calls.__setitem__("commit", calls["commit"] + 1))
    monkeypatch.setattr(r, "embed_chunks", lambda chunks: [{**ch, "vector": [0.0]} for ch in chunks])
    monkeypatch.setattr(r, "_parse_file",
                        lambda p: [{"type": "table", "text": f"row {i}"} for i in range(n_raw)])
    monkeypatch.setattr(r, "EMBED_BATCH", batch)
    monkeypatch.setattr(r, "INFLIGHT_DIR", tmp_path / "inflight")
    monkeypatch.setattr(r, "BREADCRUMB_MIN_MB", 0.0)   # force a breadcrumb so we can count touches
    monkeypatch.setattr(r, "_breadcrumb_touch",
                        lambda tag: calls.__setitem__("touch", calls["touch"] + 1))
    return calls


def _run(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    now = datetime.now(timezone.utc)
    return r.process_file(object(), str(f), onedrive_item_id="id1", file_name="x.csv",
                          file_path="/x.csv", modified_date=now, created_date=now)


def test_streaming_multi_batch(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, n_raw=12, batch=5)
    res = _run(tmp_path)
    assert res["status"] == "ok"
    assert res["chunks"] == 12
    assert calls["delete"] == 1                                   # old deleted exactly once
    assert calls["commit"] == 1                                   # committed once, at the end
    assert [c for _, c in calls["upserts"]] == [False, False, False]  # 3 batches, all uncommitted
    assert [nn for nn, _ in calls["upserts"]] == [5, 5, 2]
    assert calls["indices"] == list(range(12))                   # chunk_index globally contiguous
    assert calls["touch"] == 3                                    # progress signalled per batch


def test_streaming_single_batch_matches_old_shape(monkeypatch, tmp_path):
    # A normal small file (< one batch) => exactly one upsert + one commit.
    calls = _setup(monkeypatch, tmp_path, n_raw=4, batch=5000)
    res = _run(tmp_path)
    assert res["status"] == "ok"
    assert res["chunks"] == 4
    assert calls["delete"] == 1
    assert calls["commit"] == 1
    assert calls["upserts"] == [(4, False)]


def test_streaming_empty_no_delete_no_commit(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, n_raw=0, batch=5)
    res = _run(tmp_path)
    assert res["status"] == "empty"
    assert calls["delete"] == 0     # never deleted (no batch ran) => old chunks preserved
    assert calls["commit"] == 0
    assert calls["upserts"] == []


def test_streaming_embed_failure_quarantines_and_cleans(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, n_raw=12, batch=5)
    recorded = {}
    monkeypatch.setattr(
        r, "_record_failure",
        lambda o, nm, h, err, immediate=False: (recorded.update(err=err, immediate=immediate) or 3),
    )
    state = {"n": 0}

    def flaky_embed(chunks):
        state["n"] += 1
        if state["n"] == 2:          # fail the 2nd batch, after batch 1 is written
            raise RuntimeError("boom")
        return [{**ch, "vector": [0.0]} for ch in chunks]

    monkeypatch.setattr(r, "embed_chunks", flaky_embed)
    res = _run(tmp_path)
    assert res["status"] == "error"
    assert "CHUNK/EMBED" in recorded["err"]
    assert recorded["immediate"] is True
    assert calls["delete"] == 2     # 1 before first write + 1 cleanup of the partial
    assert calls["commit"] == 0     # never committed
