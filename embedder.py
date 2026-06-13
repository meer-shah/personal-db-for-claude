import threading

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


def warmup(timeout: float = 180.0) -> None:
    """
    Load the embedding model ONCE, at startup, bounded by a timeout.

    Loading the model can reach out to HuggingFace (revalidation, or a
    first-time download). If that network call hangs with no timeout (a
    half-closed connection — the CLOSE-WAIT socket seen in the 45-min
    production hang), a *lazy* load triggered inside a worker thread wedges
    the whole indexer with no way to recover.

    Loading here, in the main thread before any worker starts, means no worker
    ever triggers a network load; and the join(timeout) guarantees we fail
    LOUDLY (caller exits so systemd retries) instead of hanging forever.
    HF_HUB_DOWNLOAD_TIMEOUT (set by the runner before this import) bounds the
    underlying HF call so a cached model still loads even with no network.
    """
    if _model is not None:
        return
    box: dict = {}

    def _load():
        try:
            _get_model()
        except Exception as e:
            box["err"] = e

    t = threading.Thread(target=_load, name="model-warmup", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(
            f"model load exceeded {timeout:.0f}s (likely a HuggingFace network "
            f"hang); set HF_HUB_OFFLINE=1 if the model is already cached"
        )
    if "err" in box:
        raise box["err"]


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
