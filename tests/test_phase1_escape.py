import json
from ingestion import runner


def test_phase1_interrupt_counter_climbs_and_resets(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PRIORITY_FILE", tmp_path / "priority.json")
    (tmp_path / "priority.json").write_text(json.dumps({"folders": ["/X"], "files_done": 0}))
    runner._priority_cache = None  # force reload from the temp ledger

    # 3 no-progress interrupts -> counter climbs to the escape threshold
    counts = [runner._record_phase1_interrupt(False) for _ in range(3)]
    assert counts == [1, 2, 3]
    assert counts[-1] >= runner.PHASE1_MAX_INTERRUPTS   # would trigger clear-priority

    # real progress resets the counter (a slow-but-working Phase 1 is not abandoned)
    assert runner._record_phase1_interrupt(True) == 0
    runner._priority_cache = None


def test_no_counter_without_active_priority(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "PRIORITY_FILE", tmp_path / "none.json")
    runner._priority_cache = None
    assert runner._record_phase1_interrupt(False) == 0   # no active priority -> no-op
    runner._priority_cache = None
