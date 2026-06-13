import tiktoken

_enc = tiktoken.get_encoding("cl100k_base")
MAX_TOKENS = 512
OVERLAP = 50


def chunk_texts(texts: list[dict]) -> list[dict]:
    """
    Accept the raw output from any parser (list of dicts with 'type' and 'text')
    and return a flat list of chunk dicts ready for embedding.

    Text chunks are split at MAX_TOKENS with OVERLAP-token overlap.

    Tables are normally emitted as one chunk each (parsers already batch rows
    under their own token budget, so a normal table fits and is byte-for-byte
    unchanged). As a HARD SAFETY CAP, any single chunk -- table OR text --
    larger than MAX_TOKENS is split before it can reach the embedder. A single
    oversized chunk (a CSV/XLSX row with a huge cell, a giant PDF page) would
    otherwise force the embedder to tokenize one enormous string, spiking RSS
    into the recycle ceiling and killing the process mid-embed. Splitting
    guarantees every chunk handed to the model is bounded.

    Each output dict preserves all keys from the input dict and adds:
        chunk_index - position of this chunk within the file's output list
    """
    result: list[dict] = []
    idx = 0

    for item in texts:
        chunk_type = item.get("type", "text")
        text = item["text"]
        extra = {k: v for k, v in item.items() if k not in ("type", "text")}

        # Tables: no overlap (rows are self-contained). Text: sliding window
        # with overlap. Both are capped at MAX_TOKENS.
        overlap = 0 if chunk_type == "table" else OVERLAP
        for piece in _split_tokens(text, overlap):
            result.append({"type": chunk_type, "text": piece, "chunk_index": idx, **extra})
            idx += 1

    return result


def _split_tokens(text: str, overlap: int) -> list[str]:
    """
    Split text into <=MAX_TOKENS-token pieces.

    Returns [text] unchanged when it already fits (the overwhelmingly common
    path), so normal chunks are byte-for-byte identical to before. Only
    pathologically large chunks are split.
    """
    tokens = _enc.encode(text)
    if len(tokens) <= MAX_TOKENS:
        return [text]

    pieces: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + MAX_TOKENS, len(tokens))
        pieces.append(_enc.decode(tokens[start:end]))
        if end == len(tokens):
            break
        start = end - overlap if overlap else end
    return pieces


def _split_text(text: str) -> list[str]:
    """Backwards-compatible alias for the text splitter (with overlap)."""
    return _split_tokens(text, OVERLAP)
