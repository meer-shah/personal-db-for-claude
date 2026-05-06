"""
index_status tool — returns indexing progress from /var/pkp/status.json
plus live Qdrant point count.
"""

import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from qdrant_client import QdrantClient

from tools_mcp.auth import require_bearer

router = APIRouter()
COLLECTION = "pkp_chunks"

REPO_ROOT = Path(__file__).resolve().parent.parent

# Use whichever status file exists (prod path first, then dev fallback)
def _status_file() -> Path:
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
    errors:             list[IndexError]


def _get_qdrant() -> QdrantClient:
    host = os.getenv("QDRANT_HOST", "localhost")
    port = int(os.getenv("QDRANT_PORT", 6333))
    return QdrantClient(host=host, port=port)


@router.get("/tools/index_status", response_model=IndexStatusResponse)
def index_status(_: None = Depends(require_bearer)) -> IndexStatusResponse:
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

    errors = [
        IndexError(file=e.get("file", ""), reason=e.get("reason", ""))
        for e in status.get("errors", [])
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
        errors             = errors,
    )
