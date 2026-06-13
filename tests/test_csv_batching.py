"""
CSV batching — a large CSV must NOT become one giant chunk, and (v2) is parsed
as a STREAM (generator) so it is never fully materialized.

Regression test for the production incidents:
  - v1: two ~15-17 MB SAP CSVs became one ~15 MB chunk that killed the embedder.
  - v2: a 252 MB CSV (~1.1 M chunks) was held all-at-once and the indexer hung.

_parse_csv now yields TABLE chunks; parse_plain returns that generator for .csv
(a list for .txt/.md). Callers that need a concrete list wrap it in list().
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import tiktoken
from parsers.plain_parser import parse_plain, _parse_csv
from chunker import chunk_texts, MAX_TOKENS

_enc = tiktoken.get_encoding("cl100k_base")


def _tokens(text: str) -> int:
    return len(_enc.encode(text))


def _make_csv(path, rows: int) -> None:
    lines = ["col_a,col_b,col_c,col_d"]
    for i in range(rows):
        lines.append(f"value_{i}_aaaa,value_{i}_bbbb,value_{i}_cccc,value_{i}_dddd")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_csv_is_a_generator():
    assert inspect.isgeneratorfunction(_parse_csv)


def test_small_csv_single_chunk(tmp_path):
    p = tmp_path / "small.csv"
    _make_csv(p, 3)
    chunks = list(parse_plain(str(p)))
    assert len(chunks) == 1
    assert chunks[0]["type"] == "table"
    assert "value_0_aaaa" in chunks[0]["text"]


def test_large_csv_is_batched_not_giant(tmp_path):
    p = tmp_path / "big.csv"
    _make_csv(p, 5000)
    chunks = list(parse_plain(str(p)))
    assert len(chunks) > 50, "a large CSV must be split into many bounded chunks, not one"
    for c in chunks:
        assert c["type"] == "table"
        assert _tokens(c["text"]) < 1000   # no chunk anywhere near 'giant'


def test_large_csv_no_data_loss(tmp_path):
    p = tmp_path / "big.csv"
    _make_csv(p, 5000)
    joined = "\n".join(c["text"] for c in parse_plain(str(p)))
    assert "value_0_aaaa" in joined        # first row
    assert "value_4999_dddd" in joined     # last row


def test_header_repeated_in_every_chunk(tmp_path):
    p = tmp_path / "big.csv"
    _make_csv(p, 5000)
    for c in parse_plain(str(p)):
        assert c["text"].startswith("TABLE: col_a | col_b | col_c | col_d")


def test_full_pipeline_every_chunk_within_hard_cap(tmp_path):
    # parse -> chunk: every chunk handed to the embedder is <= MAX_TOKENS, and
    # chunk_index is globally contiguous when streamed batch-by-batch.
    p = tmp_path / "big.csv"
    _make_csv(p, 5000)
    raw = list(parse_plain(str(p)))
    out = chunk_texts(raw)
    assert len(out) > 50
    for c in out:
        assert _tokens(c["text"]) <= MAX_TOKENS
    assert [c["chunk_index"] for c in out] == list(range(len(out)))


def test_empty_csv_returns_nothing(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    assert list(parse_plain(str(p))) == []


def test_header_only_csv_returns_nothing(tmp_path):
    p = tmp_path / "headeronly.csv"
    p.write_text("col_a,col_b\n", encoding="utf-8")
    assert list(parse_plain(str(p))) == []
