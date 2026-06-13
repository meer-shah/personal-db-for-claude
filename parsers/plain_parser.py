import csv
import sys

import tiktoken

_enc = tiktoken.get_encoding("cl100k_base")
_MAX_TOKENS = 400  # leave headroom for the model's 256 word-piece limit

# SAP/CSV exports can contain a single cell larger than Python's 128 KB default
# csv field limit. Without this, ONE oversized cell raises
# "field larger than field limit" and quarantines the WHOLE file (silently
# dropped). Raise the limit so the row parses; the chunker's hard token cap then
# splits the resulting large chunk normally. Guard OverflowError on platforms
# whose C long is smaller than sys.maxsize.
_fsl = sys.maxsize
while True:
    try:
        csv.field_size_limit(_fsl)
        break
    except OverflowError:
        _fsl //= 10


def parse_plain(file_path: str):
    """
    Parse .txt and .md files as a single text chunk (returned as a list; the
    chunker splits it). Parse .csv files as a STREAM of TABLE chunks via a
    generator, each batched under _MAX_TOKENS, so a large CSV never becomes one
    giant chunk AND is never fully materialized in memory.

    Returns an *iterable* of dicts with keys:
        type  - 'text' or 'table'
        text  - raw string content
    (a list for .txt/.md, a generator for .csv — the streaming ingest pipeline
    consumes either with itertools batching.)
    """
    lower = file_path.lower()

    if lower.endswith(".csv"):
        return _parse_csv(file_path)

    # .txt and .md
    with open(file_path, encoding="utf-8", errors="replace") as f:
        content = f.read().strip()

    if not content:
        return []
    return [{"type": "text", "text": content}]


def _parse_csv(file_path: str):
    """
    Stream a CSV into TABLE chunks of at most _MAX_TOKENS tokens each (mirrors
    the xlsx parser), yielding one chunk at a time.

    Why a generator: a 252 MB SAP export is ~1.1 M chunks. Building that whole
    list before embedding spiked memory; embedding it all-at-once spiked it
    further and the process was killed mid-embed. Yielding lets the ingest
    pipeline embed + upsert each batch and free it, so peak memory is bounded
    by one batch regardless of file size. The header line is repeated at the
    top of each chunk so each chunk is self-describing for retrieval; DictReader
    streams row-by-row so the raw file is never fully held either.
    """
    with open(file_path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        headers = [h for h in (reader.fieldnames or []) if h]
        if not headers:
            return

        header_line = "TABLE: " + " | ".join(headers)
        base_tokens = len(_enc.encode(header_line))

        batch_lines: list[str] = []
        batch_tokens = base_tokens

        for row in reader:
            row_lines = [f"{h}: {str(row.get(h, '') or '').strip()}" for h in headers]
            row_lines.append("---")
            row_tokens = len(_enc.encode("\n".join(row_lines)))

            # Start a new chunk when adding this row would blow the budget.
            # A single row larger than the budget becomes its own chunk; the
            # chunker's hard cap splits it further if even one row is huge.
            if batch_lines and batch_tokens + row_tokens > _MAX_TOKENS:
                yield {"type": "table", "text": header_line + "\n" + "\n".join(batch_lines)}
                batch_lines = []
                batch_tokens = base_tokens

            batch_lines.extend(row_lines)
            batch_tokens += row_tokens

        if batch_lines:
            yield {"type": "table", "text": header_line + "\n" + "\n".join(batch_lines)}
