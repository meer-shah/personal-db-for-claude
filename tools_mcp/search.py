"""
search_documents tool — embed query → Qdrant top-N (adaptive) → rerank → top-k → context expansion.
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Filter,
    FieldCondition,
    MatchValue,
    MatchAny,
    MatchText,
    Range,
    SearchParams,
    QuantizationSearchParams,
)

from embedder import embed_query
from tools_mcp.auth import require_bearer

router  = APIRouter()
limiter = Limiter(key_func=get_remote_address)

COLLECTION           = "pkp_chunks"
CONFIDENCE_THRESHOLD = 0.70
CANDIDATE_POOL_DEFAULT = 150   # adaptive — Claude can override per query (1..500)
CANDIDATE_POOL_MAX     = 500
HNSW_EF              = 256
RERANK_BATCH_SIZE    = 64
CONTEXT_NEIGHBOURS   = 1   # fetch chunk_index ±1 around each top result

# Audit log — one line per query; falls back to project dir if /var/log/pkp not writable
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PREFERRED_AUDIT_LOG = Path("/var/log/pkp/audit.log")
_FALLBACK_AUDIT_LOG  = _REPO_ROOT / "logs" / "audit.log"
_audit_logger = logging.getLogger("pkp.audit")

def _setup_audit_log() -> None:
    if _audit_logger.handlers:
        return
    for candidate in (_PREFERRED_AUDIT_LOG, _FALLBACK_AUDIT_LOG):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(candidate)
            handler.setFormatter(logging.Formatter("%(asctime)s\t%(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"))
            _audit_logger.addHandler(handler)
            _audit_logger.setLevel(logging.INFO)
            _audit_logger.propagate = False
            return
        except PermissionError:
            continue

_setup_audit_log()


# ── Request / Response models ─────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query:             str
    top_k:             int        = Field(default=5, ge=1, le=15)
    file_type_filter:  str | None = None   # e.g. "docx", "pdf"
    folder_filter:     str | None = None   # prefix match on file_path
    author_filter:     str | None = None   # substring match on author
    date_from:         str | None = None   # ISO date string  e.g. "2025-01-01"
    date_to:           str | None = None   # ISO date string  e.g. "2025-12-31"
    candidate_pool:    int        = Field(default=CANDIDATE_POOL_DEFAULT, ge=1, le=CANDIDATE_POOL_MAX)
    # Number of candidates to retrieve from Qdrant before reranking. Use ~50 for narrow lookups,
    # 150 for normal queries, up to 500 for broad/exploratory questions across a 1TB library.

    # Scoped search: when set, only chunks belonging to one of these file_paths
    # are considered. Use this for "compare these N documents" queries — Claude
    # passes the file_paths returned from a prior search, and the next search
    # is restricted to chunks within those files. Cap at 20 to keep the Qdrant
    # filter fast (more than 20 docs at once is rarely a real comparison case
    # and is usually better expressed as a folder_filter).
    file_paths:        list[str] | None = Field(default=None, max_length=20)


class SearchResult(BaseModel):
    text:          str   # the matched chunk merged with neighbouring chunks for context
    score:         float
    confident:     bool
    chunk_index:   int | None = None
    file_name:     str
    file_path:     str
    file_type:     str
    chunk_type:    str
    page_number:   int | None
    slide_number:  int | None
    sheet_name:    str | None
    modified_date: str | None
    author:        str | None


class SearchResponse(BaseModel):
    results:       list[SearchResult]
    query:         str
    total_found:   int
    confident:     bool   # False if best score < threshold


# ── Helpers ───────────────────────────────────────────────────────────────────

def _iso_to_epoch(iso: str) -> float:
    """Convert an ISO date/datetime string to a UTC unix timestamp (float)."""
    iso = iso.strip()
    # Accept bare dates like "2025-01-01" by appending midnight UTC
    if len(iso) == 10:
        iso += "T00:00:00+00:00"
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).timestamp()
    except ValueError:
        # If the string is not parseable return 0 so the filter is permissive
        return 0.0


def _build_filter(req: SearchRequest) -> Filter | None:
    conditions = []

    if req.file_type_filter:
        conditions.append(
            FieldCondition(key="file_type", match=MatchValue(value=req.file_type_filter.lower()))
        )

    # Folder prefix — use MatchText so "/SAP/Proposals" matches any file under that folder
    if req.folder_filter:
        conditions.append(
            FieldCondition(key="file_path", match=MatchText(text=req.folder_filter))
        )

    # Author substring match
    if req.author_filter:
        conditions.append(
            FieldCondition(key="author", match=MatchText(text=req.author_filter))
        )

    # Scoped search: restrict to specific file_paths (exact match, any of N).
    # MatchAny does an OR across the list — a chunk matches if its file_path
    # equals any of the provided paths.
    if req.file_paths:
        conditions.append(
            FieldCondition(key="file_path", match=MatchAny(any=req.file_paths))
        )

    # Date range — stored as unix epoch float (modified_date_ts) so Range works correctly
    date_range: dict = {}
    if req.date_from:
        date_range["gte"] = _iso_to_epoch(req.date_from)
    if req.date_to:
        # Include the entire end day by moving to 23:59:59
        date_range["lte"] = _iso_to_epoch(req.date_to.strip()[:10] + "T23:59:59+00:00")
    if date_range:
        conditions.append(
            FieldCondition(key="modified_date_ts", range=Range(**date_range))
        )

    if not conditions:
        return None
    return Filter(must=conditions)


def _get_qdrant() -> QdrantClient:
    host = os.getenv("QDRANT_HOST", "localhost")
    port = int(os.getenv("QDRANT_PORT", 6333))
    return QdrantClient(host=host, port=port)


# ── Reranker (BGE) ────────────────────────────────────────────────────────────

_reranker = None

def _get_reranker():
    global _reranker
    if _reranker is None:
        try:
            from sentence_transformers import CrossEncoder
            _reranker = CrossEncoder("BAAI/bge-reranker-base")
        except Exception:
            _reranker = False
    return _reranker if _reranker is not False else None


def _rerank(query: str, hits: list, top_k: int) -> list:
    reranker = _get_reranker()
    if reranker is None or len(hits) <= top_k:
        return hits[:top_k]
    pairs  = [(query, h.payload.get("text", "")) for h in hits]
    scores = reranker.predict(pairs, batch_size=RERANK_BATCH_SIZE)
    ranked = sorted(zip(scores, hits), key=lambda x: x[0], reverse=True)
    return [h for _, h in ranked[:top_k]]


def _expand_context(client: QdrantClient, hits: list) -> dict[tuple[str, int], str]:
    """For each (file_path, chunk_index) hit, fetch neighbours ±CONTEXT_NEIGHBOURS
    from the same file and return a map (file_path, chunk_index) -> merged_text."""
    if not hits or CONTEXT_NEIGHBOURS <= 0:
        return {}

    # Group target chunk indices by file_path so we issue one scroll per file
    targets: dict[str, set[int]] = {}
    wanted: dict[str, set[int]] = {}
    for h in hits:
        p = h.payload or {}
        fp = p.get("file_path")
        ci = p.get("chunk_index")
        if fp is None or ci is None:
            continue
        targets.setdefault(fp, set()).add(ci)
        for delta in range(-CONTEXT_NEIGHBOURS, CONTEXT_NEIGHBOURS + 1):
            wanted.setdefault(fp, set()).add(ci + delta)

    merged: dict[tuple[str, int], str] = {}
    for fp, indices in targets.items():
        lo = min(wanted[fp])
        hi = max(wanted[fp])
        flt = Filter(must=[
            FieldCondition(key="file_path", match=MatchValue(value=fp)),
            FieldCondition(key="chunk_index", range=Range(gte=lo, lte=hi)),
        ])
        # Scroll all matching neighbours for this file
        chunks_by_index: dict[int, str] = {}
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=COLLECTION,
                scroll_filter=flt,
                with_payload=True,
                with_vectors=False,
                limit=64,
                offset=offset,
            )
            for pt in points:
                payload = pt.payload or {}
                idx = payload.get("chunk_index")
                if idx is None:
                    continue
                chunks_by_index[idx] = payload.get("text", "")
            if offset is None:
                break

        for ci in indices:
            ordered = [chunks_by_index[i] for i in range(ci - CONTEXT_NEIGHBOURS,
                                                          ci + CONTEXT_NEIGHBOURS + 1)
                       if i in chunks_by_index]
            if ordered:
                merged[(fp, ci)] = "\n\n".join(ordered)
    return merged


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/tools/search_documents", response_model=SearchResponse)
@limiter.limit("100/minute")
def search_documents(
    request: Request,
    req: SearchRequest,
    _: None = Depends(require_bearer),
) -> SearchResponse:
    t0 = time.monotonic()
    vector = embed_query(req.query)
    client = _get_qdrant()

    response = client.query_points(
        collection_name=COLLECTION,
        query=vector,
        limit=req.candidate_pool,
        query_filter=_build_filter(req),
        with_payload=True,
        search_params=SearchParams(
            hnsw_ef=HNSW_EF,
            quantization=QuantizationSearchParams(
                rescore=True,
                oversampling=2.0,
            ),
        ),
    )
    hits = response.points

    if not hits:
        return SearchResponse(
            results=[], query=req.query, total_found=0, confident=False
        )

    top = _rerank(req.query, hits, req.top_k)

    # Context window expansion — merge each top hit with its neighbouring chunks
    expanded = _expand_context(client, top)

    results = []
    for h in top:
        p = h.payload or {}
        fp = p.get("file_path", "")
        ci = p.get("chunk_index")
        merged_text = expanded.get((fp, ci), p.get("text", ""))
        results.append(SearchResult(
            text          = merged_text,
            score         = round(float(h.score), 4),
            confident     = h.score >= CONFIDENCE_THRESHOLD,
            chunk_index   = ci,
            file_name     = p.get("file_name", ""),
            file_path     = p.get("file_path", ""),
            file_type     = p.get("file_type", ""),
            chunk_type    = p.get("chunk_type", "text"),
            page_number   = p.get("page_number"),
            slide_number  = p.get("slide_number"),
            sheet_name    = p.get("sheet_name"),
            modified_date = p.get("modified_date"),
            author        = p.get("author"),
        ))

    overall_confident = bool(results and results[0].score >= CONFIDENCE_THRESHOLD)
    latency_ms = round((time.monotonic() - t0) * 1000)
    _audit_logger.info(
        "SEARCH\tquery=%r\tpool=%d\tn_results=%d\tconfident=%s\tlatency_ms=%d",
        req.query, req.candidate_pool, len(results), overall_confident, latency_ms,
    )
    return SearchResponse(
        results     = results,
        query       = req.query,
        total_found = len(hits),
        confident   = overall_confident,
    )
