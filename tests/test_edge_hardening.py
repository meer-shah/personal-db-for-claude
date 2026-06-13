"""
Edge-case hardening surfaced by the whole-pipeline validation gate:
  1. tiktoken BPE wedges on a huge whitespace-free blob -> the chunker now
     encodes in bounded char windows.
  2. a CSV cell > 128 KB hit Python's default csv field limit and quarantined
     the whole file -> the limit is raised so the row parses and the chunker
     caps the resulting chunk.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import tiktoken
from chunker import chunk_texts, MAX_TOKENS

_enc = tiktoken.get_encoding("cl100k_base")


def _toks(t):
    return len(_enc.encode(t))


def test_no_whitespace_blob_chunks_fast_and_bounded():
    # ~2.2MB single contiguous no-whitespace run (minified/base64-shaped).
    blob = "nowhitespacejustonehugetoken" * 80000
    t = time.time()
    out = chunk_texts([{"type": "text", "text": blob}])
    dt = time.time() - t
    assert dt < 25, f"chunking a no-whitespace blob took {dt:.1f}s (tiktoken wedge not fixed)"
    assert len(out) > 1
    for c in out:
        assert _toks(c["text"]) <= 2 * MAX_TOKENS   # <=cap by construction; allow small re-encode drift


def test_csv_with_oversized_cell_parses_not_quarantines(tmp_path):
    from parsers.plain_parser import parse_plain
    p = tmp_path / "huge_cell.csv"
    p.write_text("h1,h2\nx," + ("z" * 500_000) + "\n", encoding="utf-8")  # 500KB cell > 128KB default
    chunks = list(parse_plain(str(p)))      # must NOT raise (was: field limit error -> quarantine)
    assert len(chunks) >= 1
    out = chunk_texts(chunks)
    assert len(out) >= 1
    for c in out:
        assert _toks(c["text"]) <= 2 * MAX_TOKENS   # <=cap by construction; allow small re-encode drift


def test_normal_small_text_unchanged():
    # the common path must be byte-for-byte identical (no windowing kicks in)
    out = chunk_texts([{"type": "text", "text": "Just a short normal sentence."}])
    assert len(out) == 1
    assert out[0]["text"] == "Just a short normal sentence."
