"""
CSV batching — a large CSV must NOT become one giant chunk.

This is the regression test for the production incident where two ~15-17 MB SAP
export CSVs were each turned into a SINGLE ~15 MB chunk. Embedding that single
chunk spiked RSS into the recycle ceiling and killed the process mid-embed
(before it could be quarantined), so the indexer looped on it for 52 hours.

The parser must batch rows under its own token budget, and the full
parse -> chunk pipeline must keep every chunk within the chunker's hard cap.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import tiktoken
from parsers.plain_parser import parse_plain, _MAX_TOKENS
from chunker import chunk_texts, MAX_TOKENS

_enc = tiktoken.get_encoding("cl100k_base")


def _tokens(text: str) -> int:
    return len(_enc.encode(text))


def _make_csv(path, rows: int) -> None:
    lines = ["col_a,col_b,col_c,col_d"]
    for i in range(rows):
        lines.append(f"value_{i}_aaaa,value_{i}_bbbb,value_{i}_cccc,value_{i}_dddd")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_small_csv_single_chunk(tmp_path):
    p = tmp_path / "small.csv"
    _make_csv(p, 3)
    chunks = parse_plain(str(p))
    assert len(chunks) == 1
    assert chunks[0]["type"] == "table"
    assert "value_0_aaaa" in chunks[0]["text"]


def test_large_csv_is_batched_not_giant(tmp_path):
    p = tmp_path / "big.csv"
    _make_csv(p, 5000)
    chunks = parse_plain(str(p))
    assert len(chunks) > 50, "a large CSV must be split into many bounded chunks, not one"
    for c in chunks:
        assert c["type"] == "table"
        # The whole point: no chunk is anywhere near 'giant'. (The chunker
        # enforces the hard <=MAX_TOKENS guarantee; here we just prove the
        # parser already batches tightly.)
        assert _tokens(c["text"]) < 1000


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
    # parse -> chunk: every chunk handed to the embedder is <= MAX_TOKENS.
    p = tmp_path / "big.csv"
    _make_csv(p, 5000)
    out = chunk_texts(parse_plain(str(p)))
    assert len(out) > 50
    for c in out:
        assert _tokens(c["text"]) <= MAX_TOKENS
    assert [c["chunk_index"] for c in out] == list(range(len(out)))


def test_empty_csv_returns_nothing(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    assert parse_plain(str(p)) == []


def test_header_only_csv_returns_nothing(tmp_path):
    p = tmp_path / "headeronly.csv"
    p.write_text("col_a,col_b\n", encoding="utf-8")
    assert parse_plain(str(p)) == []
