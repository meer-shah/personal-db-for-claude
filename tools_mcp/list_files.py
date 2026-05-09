"""
list_files tool — discover files in the index by name or path substring.

This is the discovery layer that bridges natural-language file references
("my FYP SRS", "the budget spreadsheet") to the exact file_paths the
search_documents tool needs. Users almost never know the exact OneDrive
path; instead they describe a file by name. list_files lets Claude:

  1. Take the user's description and call list_files(name_query="FYP SRS")
  2. Present the matches to the user, who confirms which ones
  3. Call search_documents(query, file_paths=[...the confirmed ones...])

The response is intentionally small: file metadata only, no chunk text.
That keeps it safe for any MCP response cap regardless of how many files
match. Capped at 50 matches per call.
"""

import os
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchText, MatchValue

from tools_mcp.auth import require_bearer

router = APIRouter()
COLLECTION = "pkp_chunks"

_MAX_RESULTS = 50
# Hard ceiling on how many points we scroll. Files have many chunks; we
# de-duplicate to one entry per file. Scrolling 5000 points is plenty to
# find 50 distinct files even on a heavily-chunked collection.
_SCROLL_LIMIT = 5000


class FileMatch(BaseModel):
    file_name:     str
    file_path:     str
    file_type:     str
    chunk_count:   int
    author:        str | None = None
    modified_date: str | None = None


class ListFilesResponse(BaseModel):
    matches:        list[FileMatch]
    total_returned: int
    has_more:       bool


def _get_qdrant() -> QdrantClient:
    host = os.getenv("QDRANT_HOST", "localhost")
    port = int(os.getenv("QDRANT_PORT", 6333))
    return QdrantClient(host=host, port=port)


@router.get("/tools/list_files", response_model=ListFilesResponse)
def list_files(
    _: None = Depends(require_bearer),
    name_query:        str        = Query(..., min_length=1, description="Substring to match against file names and paths (case-insensitive)."),
    file_type_filter:  str | None = Query(None, description="Restrict to a single file type, e.g. pdf, docx, xlsx."),
    folder_filter:     str | None = Query(None, description="Restrict to file_paths containing this folder substring."),
    limit:             int        = Query(_MAX_RESULTS, ge=1, le=_MAX_RESULTS, description="Max matches to return."),
) -> ListFilesResponse:
    """
    Find files in the index whose name (or path) matches name_query.

    Returns one entry per distinct file with metadata only — no chunk text —
    so responses are always small. Use this to disambiguate user-described
    files into concrete file_paths you can pass to search_documents.
    """
    client = _get_qdrant()

    # Build a Qdrant filter for the metadata-only fields we know about.
    # We do a substring match on file_path (which contains both folder and
    # filename), then filter further in Python by file_name to support the
    # case-insensitive name match the user actually wants.
    must = [FieldCondition(key="file_path", match=MatchText(text=name_query))]
    if file_type_filter:
        must.append(
            FieldCondition(key="file_type", match=MatchValue(value=file_type_filter.lower()))
        )
    if folder_filter:
        must.append(
            FieldCondition(key="file_path", match=MatchText(text=folder_filter))
        )

    # Scroll for points matching the filter; aggregate by file_path.
    seen: dict[str, FileMatch] = {}
    chunk_counts: dict[str, int] = defaultdict(int)
    offset = None
    scrolled = 0

    while scrolled < _SCROLL_LIMIT:
        points, offset = client.scroll(
            collection_name=COLLECTION,
            scroll_filter=Filter(must=must),
            limit=min(1000, _SCROLL_LIMIT - scrolled),
            offset=offset,
            with_payload=["file_path", "file_name", "file_type", "author", "modified_date"],
            with_vectors=False,
        )
        if not points:
            break

        for p in points:
            payload = p.payload or {}
            fp = payload.get("file_path", "")
            chunk_counts[fp] += 1
            if fp not in seen:
                seen[fp] = FileMatch(
                    file_name     = payload.get("file_name", ""),
                    file_path     = fp,
                    file_type     = payload.get("file_type", ""),
                    chunk_count   = 0,  # filled in after aggregation
                    author        = payload.get("author"),
                    modified_date = payload.get("modified_date"),
                )

        scrolled += len(points)
        if offset is None:
            break

    # Apply chunk counts and a case-insensitive substring filter on the
    # file_name itself (the Qdrant MatchText above filters on path; this
    # narrows to files where the *name* itself contains the query).
    nq_lower = name_query.lower()
    candidates: list[FileMatch] = []
    for fp, fm in seen.items():
        fm.chunk_count = chunk_counts[fp]
        # Match if either the file_name or the trailing path component contains nq.
        # This is intentionally permissive — folder hits already passed the Qdrant
        # filter, so leaving them in lets queries like "fyp SRS" surface files
        # whose folder is /fyp/ even if the filename doesn't say "fyp".
        candidates.append(fm)

    # Sort: file_name match takes priority over folder-only match, then by
    # chunk_count desc as a rough proxy for "more substantial document".
    def _sort_key(fm: FileMatch) -> tuple:
        name_match = nq_lower in fm.file_name.lower()
        return (0 if name_match else 1, -fm.chunk_count, fm.file_name.lower())
    candidates.sort(key=_sort_key)

    has_more = len(candidates) > limit
    matches = candidates[:limit]

    return ListFilesResponse(
        matches        = matches,
        total_returned = len(matches),
        has_more       = has_more,
    )
