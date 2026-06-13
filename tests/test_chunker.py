"""
Chunker unit tests — verify 512-token limit, 50-token overlap, and the
hard size-cap that protects the embedder from a single giant chunk.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import tiktoken
from chunker import chunk_texts, MAX_TOKENS, OVERLAP

_enc = tiktoken.get_encoding("cl100k_base")


def _tokens(text: str) -> int:
    return len(_enc.encode(text))


def test_short_text_single_chunk():
    chunks = chunk_texts([{"type": "text", "text": "Short text."}])
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Short text."
    assert chunks[0]["chunk_index"] == 0


def test_long_text_split_into_multiple_chunks():
    # Generate text that is ~3x the max tokens
    word   = "tokenword "
    target = MAX_TOKENS * 3
    text   = word * (target // _tokens(word) + 1)
    chunks = chunk_texts([{"type": "text", "text": text}])
    assert len(chunks) > 1


def test_chunk_size_never_exceeds_max():
    word  = "alpha "
    text  = word * 2000
    chunks = chunk_texts([{"type": "text", "text": text}])
    for c in chunks:
        assert _tokens(c["text"]) <= MAX_TOKENS


def test_overlap_between_consecutive_chunks():
    word  = "word "
    text  = word * 1200
    chunks = chunk_texts([{"type": "text", "text": text}])
    if len(chunks) < 2:
        pytest.skip("Text not long enough to trigger split")
    # The end of chunk N should share tokens with the start of chunk N+1
    end_of_first  = _enc.encode(chunks[0]["text"])[-OVERLAP:]
    start_of_second = _enc.encode(chunks[1]["text"])[:OVERLAP]
    assert end_of_first == start_of_second


def test_small_table_stays_one_chunk():
    # A table that fits under the cap is emitted whole (the common case) and is
    # byte-for-byte unchanged — no regression for normal tables.
    table = "TABLE: A | B\n" + "\n".join(f"A: row{i}\nB: val{i}\n---" for i in range(5))
    assert _tokens(table) <= MAX_TOKENS
    chunks = chunk_texts([{"type": "table", "text": table}])
    assert len(chunks) == 1
    assert chunks[0]["type"] == "table"
    assert chunks[0]["text"] == table


def test_oversized_table_is_split_under_cap():
    # The 15MB-CSV failure mode: a giant table must be hard-split so the
    # embedder never receives one enormous string. Every piece stays a table
    # and stays within the token cap.
    long_table = "TABLE: A | B\n" + "\n".join(f"A: row{i}\nB: val{i}\n---" for i in range(300))
    assert _tokens(long_table) > MAX_TOKENS  # sanity: this really is oversized
    chunks = chunk_texts([{"type": "table", "text": long_table}])
    assert len(chunks) > 1
    for c in chunks:
        assert c["type"] == "table"
        assert _tokens(c["text"]) <= MAX_TOKENS


def test_chunk_index_increments_correctly():
    word  = "word "
    items = [
        {"type": "table", "text": "TABLE: X\nX: row\n---"},
        {"type": "text",  "text": word * 1200},
    ]
    chunks = chunk_texts(items)
    indices = [c["chunk_index"] for c in chunks]
    assert indices == list(range(len(chunks)))


def test_extra_metadata_preserved():
    chunks = chunk_texts([{"type": "text", "text": "Hello", "page": 3, "sheet_name": "Sheet1"}])
    assert chunks[0]["page"] == 3
    assert chunks[0]["sheet_name"] == "Sheet1"


def test_extra_metadata_preserved_across_table_split():
    # When a giant table is split, every piece must keep the parser metadata
    # (e.g. sheet_name) so retrieval attribution survives.
    long_table = "TABLE: A | B\n" + "\n".join(f"A: row{i}\nB: val{i}\n---" for i in range(300))
    chunks = chunk_texts([{"type": "table", "text": long_table, "sheet_name": "Sheet1"}])
    assert len(chunks) > 1
    assert all(c["sheet_name"] == "Sheet1" for c in chunks)


def test_empty_input():
    assert chunk_texts([]) == []
