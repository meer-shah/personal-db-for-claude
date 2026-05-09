"""
index_status tool — returns indexing progress from /var/pkp/status.json
plus live Qdrant point count.

Default response is bounded in size: it returns a count of errors and a
small sample (up to 5), never the full list. Pass include_errors=true to
get a paginated slice of the full error list — this is opt-in because
on a large library (491k files) the error list can grow into the MB range
and blow past MCP client response caps (Claude's is 1 MB).

Query parameters:
  include_errors  bool, default False  Return a paginated error slice.
  errors_limit    int,  default 50     Max errors to return when include_errors=true.
  errors_offset   int,  default 0      Pagination offset when include_errors=true.
"""

import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from qdrant_client import QdrantClient

from tools_mcp.auth import require_bearer

router = APIRouter()
COLLECTION = "pkp_chunks"

REPO_ROOT = Path(__file__).resolve().parent.parent

# Hard ceiling so a misbehaving caller can't request a 100k-error payload.
_MAX_ERRORS_PER_REQUEST = 200
# How many errors to include in the default-mode sample.
_DEFAULT_ERROR_SAMPLE = 5


def _status_file() -> Path:
    """Use whichever status file exists (prod path first, then dev fallback)."""
    for p in (Path("/var/pkp/status.json"), REPO_ROOT / "logs" / "status.json"):
        if p.exists():
            return p
    return Path("/var/pkp/status.json")  # default even if missing


class IndexError(BaseModel):
    file:   str
    reason: str


class IndexStatusResponse(BaseModel):
    indexed_files:      int
    total_files:        int
    percent_complete:   float
    total_chunks:       int
    last_run_utc:       str | None
    currently_indexing: bool
    # Always populated — count of every error recorded in status.json.
    errors_total:       int
    # In default mode: first _DEFAULT_ERROR_SAMPLE errors (sample for diagnostics).
    # In include_errors mode: paginated slice of size errors_returned.
    errors:             list[IndexError]
    # Number of errors in the `errors` field above. Lets clients tell whether
    # they got a sample (≤ _DEFAULT_ERROR_SAMPLE) or a paginated page.
    errors_returned:    int
    # Pagination metadata; only meaningful when include_errors=true.
    errors_offset:      int = 0
    errors_has_more:    bool = False


def _get_qdrant() -> QdrantClient:
    host = os.getenv("QDRANT_HOST", "localhost")
    port = int(os.getenv("QDRANT_PORT", 6333))
    return QdrantClient(host=host, port=port)


@router.get("/tools/index_status", response_model=IndexStatusResponse)
def index_status(
    _: None = Depends(require_bearer),
    include_errors: bool = Query(False, description="Return a paginated slice of the full error list."),
    errors_limit:   int  = Query(50,    ge=1, le=_MAX_ERRORS_PER_REQUEST, description="Errors per page."),
    errors_offset:  int  = Query(0,     ge=0, description="Pagination offset."),
) -> IndexStatusResponse:
    status: dict = {}
    sf = _status_file()
    if sf.exists():
        try:
            status = json.loads(sf.read_text())
        except Exception:
            pass

    total_chunks = 0
    try:
        client = _get_qdrant()
        info   = client.get_collection(COLLECTION)
        total_chunks = info.points_count or 0
    except Exception:
        pass

    raw_errors = status.get("errors", []) or []
    errors_total = len(raw_errors)

    if include_errors:
        page_end   = errors_offset + errors_limit
        page_slice = raw_errors[errors_offset:page_end]
        has_more   = page_end < errors_total
    else:
        # Default: small sample for at-a-glance diagnostics, never large.
        page_slice = raw_errors[:_DEFAULT_ERROR_SAMPLE]
        has_more   = errors_total > len(page_slice)

    errors_out = [
        IndexError(file=e.get("file", ""), reason=e.get("reason", ""))
        for e in page_slice
    ]

    indexed = status.get("indexed_files", 0)
    total   = status.get("total_files", 0)
    percent = status.get("percent_complete", round(indexed / total * 100, 1) if total > 0 else 0.0)

    return IndexStatusResponse(
        indexed_files      = indexed,
        total_files        = total,
        percent_complete   = percent,
        total_chunks       = total_chunks,
        last_run_utc       = status.get("last_run_utc"),
        currently_indexing = status.get("currently_indexing", False),
        errors_total       = errors_total,
        errors             = errors_out,
        errors_returned    = len(errors_out),
        errors_offset      = errors_offset if include_errors else 0,
        errors_has_more    = has_more,
    )
