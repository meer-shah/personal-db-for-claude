import torch
from sentence_transformers import SentenceTransformer

_MODEL_NAME = "all-MiniLM-L6-v2"
_BATCH_SIZE = 64

_model: SentenceTransformer | None = None


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

    encode() runs unsynchronized — with the openpyxl read_only fix removing
    the dominant leak, the embedder's residual per-thread allocator growth
    is small enough to be acceptable in exchange for full worker concurrency.
    If RSS grows aggressively in production, lower PKP_INGEST_WORKERS rather
    than re-introduce a lock here.
    """
    model = _get_model()
    texts = [c["text"] for c in chunks]

    with torch.inference_mode():
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
    with torch.inference_mode():
        vec = model.encode(query, convert_to_numpy=True)
    return vec.tolist()
