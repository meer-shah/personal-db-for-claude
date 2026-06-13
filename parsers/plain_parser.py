import csv

import tiktoken

_enc = tiktoken.get_encoding("cl100k_base")
_MAX_TOKENS = 400  # leave headroom for the model's 256 word-piece limit


def parse_plain(file_path: str) -> list[dict]:
    """
    Parse .txt and .md files as a single text chunk (the chunker splits it).
    Parse .csv files as a STREAM of TABLE chunks, each batched under
    _MAX_TOKENS so a large CSV never becomes one giant chunk.

    Returns a list of dicts with keys:
        type  - 'text' or 'table'
        text  - raw string content
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


def _parse_csv(file_path: str) -> list[dict]:
    """
    Stream a CSV into TABLE chunks of at most _MAX_TOKENS tokens each, mirroring
    the xlsx parser.

    Why this matters: a 15 MB SAP export used to be turned into a SINGLE ~15 MB
    chunk (the chunker never split tables). The embedder then had to tokenize
    one enormous string, which spiked RSS into the recycle ceiling and KILLED
    the process mid-embed -- before the file could even be quarantined, so it
    re-attempted on every restart and the indexer looped forever. Batching rows
    keeps every chunk small and bounded, so embedding never spikes.

    The header line is repeated at the top of each chunk so each chunk is
    self-describing for retrieval. DictReader streams row-by-row, so we never
    hold the whole file as one giant string either.
    """
    chunks: list[dict] = []

    with open(file_path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        headers = [h for h in (reader.fieldnames or []) if h]
        if not headers:
            return []

        header_line = "TABLE: " + " | ".join(headers)
        base_tokens = len(_enc.encode(header_line))

        batch_lines: list[str] = []
        batch_tokens = base_tokens

        def _flush(lines, _hdr=header_line):
            if lines:
                chunks.append({"type": "table", "text": _hdr + "\n" + "\n".join(lines)})

        for row in reader:
            row_lines = [f"{h}: {str(row.get(h, '') or '').strip()}" for h in headers]
            row_lines.append("---")
            row_tokens = len(_enc.encode("\n".join(row_lines)))

            # Start a new chunk when adding this row would blow the budget.
            # A single row larger than the budget becomes its own chunk; the
            # chunker's hard cap splits it further if even one row is huge.
            if batch_lines and batch_tokens + row_tokens > _MAX_TOKENS:
                _flush(batch_lines)
                batch_lines = []
                batch_tokens = base_tokens

            batch_lines.extend(row_lines)
            batch_tokens += row_tokens

        _flush(batch_lines)

    return chunks
