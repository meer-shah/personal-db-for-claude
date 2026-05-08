import threading

import torch
from sentence_transformers import SentenceTransformer

_MODEL_NAME = "all-MiniLM-L6-v2"
_BATCH_SIZE = 64

_model: SentenceTransformer | None = None
# Serialize all encode() calls. With 12 worker threads sharing one model,
# concurrent encode() calls cause PyTorch's per-thread CPU caching allocator
# (and oneDNN scratch arenas) to grow to the high-water mark for every thread,
# multiplying memory ~12×. Embedding is vectorized internally and gains nothing
# from thread-level concurrency; serializing it keeps allocator state on a
# single thread and lets RSS stay flat across long runs.
_encode_lock = threading.Lock()


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def embed_chunks(chunks: list[dict]) -> list[dict]:
    """
    Accept chunk dicts (as produced by chunker.chunk_texts) and return the same
    dicts with a 'vector' key added (list[float], 384 dimensions).
    The model is loaded once and reused across calls.
    """
    model = _get_model()
    texts = [c["text"] for c in chunks]

    with _encode_lock, torch.inference_mode():
        all_embeddings = model.encode(
            texts,
            batch_size=_BATCH_SIZE,
            show_progress_bar=False,
            convert_to_numpy=True,
        )

    result = []
    for chunk, vec in zip(chunks, all_embeddings):
        result.append({**chunk, "vector": vec.tolist()})
    return result


def embed_query(query: str) -> list[float]:
    """Embed a single query string. Uses the same model as embed_chunks."""
    model = _get_model()
    with _encode_lock, torch.inference_mode():
        vec = model.encode(query, convert_to_numpy=True)
    return vec.tolist()
