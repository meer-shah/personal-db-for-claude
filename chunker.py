import tiktoken

_enc = tiktoken.get_encoding("cl100k_base")
MAX_TOKENS = 512
OVERLAP = 50


def chunk_texts(texts: list[dict]) -> list[dict]:
    """
    Accept the raw output from any parser (list of dicts with 'type' and 'text')
    and return a flat list of chunk dicts ready for embedding.

    Tables are never split — they always become exactly one chunk.
    Text chunks are split at MAX_TOKENS with OVERLAP-token overlap.

    Each output dict preserves all keys from the input dict and adds:
        chunk_index — position of this chunk within the file's output list
    """
    result: list[dict] = []
    idx = 0

    for item in texts:
        chunk_type = item.get("type", "text")
        text = item["text"]
        extra = {k: v for k, v in item.items() if k not in ("type", "text")}

        if chunk_type == "table":
            result.append({"type": chunk_type, "text": text, "chunk_index": idx, **extra})
            idx += 1
        else:
            for piece in _split_text(text):
                result.append({"type": chunk_type, "text": piece, "chunk_index": idx, **extra})
                idx += 1

    return result


def _split_text(text: str) -> list[str]:
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
        start = end - OVERLAP
    return pieces
