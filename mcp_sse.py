"""
MCP transport — exposes PKP tools via FastMCP.
Provides Streamable HTTP at /mcp (used by Claude.ai) and legacy SSE at /sse.
The session manager must be started inside the FastAPI lifespan.
"""

import json
import os
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP

_mcp = FastMCP("PKP Knowledge Base", stateless_http=True, json_response=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _token() -> str:
    return os.getenv("MCP_BEARER_TOKEN", "")


def _http(method: str, path: str, **kwargs) -> dict:
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {_token()}"}
    if method == "GET":
        r = client.get(path, headers=headers, **kwargs)
    else:
        r = client.post(path, headers=headers, **kwargs)
    return r.json()


# ── Tool registrations ────────────────────────────────────────────────────────

@_mcp.tool()
def search_documents(
    query: str,
    top_k: int = 5,
    file_type_filter: str | None = None,
    folder_filter: str | None = None,
    author_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    file_paths: list[str] | None = None,
) -> str:
    """
    Search the knowledge base for documents relevant to a query.
    Returns ranked text chunks with source file metadata.
    Use this to answer questions about the user's files and documents.

    For "compare these N documents on topic X" queries, pass file_paths to
    scope the search to just those files. This is much more efficient than
    calling get_document on each one — you get only the chunks relevant to
    your query, not the full documents.

    Args:
        query: The search query
        top_k: Number of results to return (1-15, default 5)
        file_type_filter: Filter by file type e.g. pdf, docx, xlsx, pptx
        folder_filter: Filter by folder path prefix e.g. /Projects/SAP
        author_filter: Filter by document author name (substring match)
        date_from: Only include documents modified after this date (ISO format e.g. 2024-01-01)
        date_to: Only include documents modified before this date (ISO format e.g. 2024-12-31)
        file_paths: Restrict search to chunks within these specific file_paths.
                    Use file_path values returned from a prior search. Up to 20 files.
                    Example: ["/drive/root:/A.pdf", "/drive/root:/B.pdf"] for a 2-doc comparison.
    """
    payload: dict[str, Any] = {"query": query, "top_k": top_k}
    if file_type_filter: payload["file_type_filter"] = file_type_filter
    if folder_filter:    payload["folder_filter"]    = folder_filter
    if author_filter:    payload["author_filter"]    = author_filter
    if date_from:        payload["date_from"]        = date_from
    if date_to:          payload["date_to"]          = date_to
    if file_paths:       payload["file_paths"]       = file_paths
    return json.dumps(_http("POST", "/tools/search_documents", json=payload), ensure_ascii=False, indent=2)


@_mcp.tool()
def get_document(file_path: str) -> str:
    """
    Retrieve the full text content of a specific document by its file path.
    Use this after search_documents to read a complete file.

    Args:
        file_path: The full file path as returned by search_documents
    """
    return json.dumps(_http("POST", "/tools/get_document", json={"file_path": file_path}), ensure_ascii=False, indent=2)


@_mcp.tool()
def index_status(
    include_errors: bool = False,
    errors_limit: int = 50,
    errors_offset: int = 0,
) -> str:
    """
    Check the current indexing status of the knowledge base.

    By default returns a compact summary: counts, percentages, total Qdrant
    chunks, and a small sample of recent errors (up to 5). The summary is
    always small (well under 1 KB) so it fits any MCP client response cap.

    For diagnostic deep-dives, set include_errors=true to get a paginated
    slice of the full error list. Use errors_limit (1-200, default 50) and
    errors_offset (default 0) to walk through pages.

    Args:
        include_errors: Return a paginated slice of the full error list.
                        Default False — returns just a 5-error sample.
        errors_limit:   Errors per page when include_errors=True (1-200).
        errors_offset:  Pagination offset when include_errors=True.
    """
    params = {
        "include_errors": "true" if include_errors else "false",
        "errors_limit":   errors_limit,
        "errors_offset":  errors_offset,
    }
    return json.dumps(_http("GET", "/tools/index_status", params=params), ensure_ascii=False, indent=2)


@_mcp.tool()
def get_upload_url(filename: str, folder_path: str) -> str:
    """
    Get a one-time signed URL for uploading any file (of any size or type) to
    the user's OneDrive. Use this when the user has a file you generated in
    your code-execution sandbox (e.g. a .pptx made with pptxgenjs, a .docx,
    a .pdf, an image) and wants it saved to their OneDrive — uploads via this
    URL preserve the file byte-for-byte (no base64 truncation).

    Two-step workflow:

      1. Call this tool with the desired filename and OneDrive folder path.
         You will receive an `upload_url` valid for 5 minutes.

      2. In your sandbox, POST the raw file bytes to that URL.
         No Authorization header is required — the URL itself is the credential.
         Set Content-Type to the correct MIME type for the file.

         Example (JavaScript / Node sandbox):
             const fs = require('fs');
             const buf = fs.readFileSync('/home/claude/report.pptx');
             const res = await fetch(uploadUrl, {
               method: 'POST',
               headers: { 'Content-Type': 'application/vnd.openxmlformats-officedocument.presentationml.presentation' },
               body: buf,
             });
             console.log(await res.json());  // { success, onedrive_url, file_id, bytes }

         Example (Python sandbox):
             import requests
             with open('/home/claude/report.pptx', 'rb') as f:
                 r = requests.post(upload_url, data=f.read(),
                                   headers={'Content-Type': 'application/vnd.openxmlformats-officedocument.presentationml.presentation'})
                 print(r.json())

    The server response contains the OneDrive web URL of the saved file.
    Files up to 100 MiB are supported; ≤4 MiB use a single PUT, larger files
    use a chunked Graph upload session automatically.

    Args:
        filename:    The filename to create on OneDrive, including extension
                     e.g. "meeting_prep.pptx", "report.docx"
        folder_path: The OneDrive folder path e.g. "/Outputs" or "/Reports/2026"
    """
    return json.dumps(_http("POST", "/tools/get_upload_url", json={
        "filename":    filename,
        "folder_path": folder_path,
    }), ensure_ascii=False, indent=2)


# ── Session manager + lifespan integration ───────────────────────────────────

_session_manager = None


def _get_session_manager():
    global _session_manager
    if _session_manager is None:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        _session_manager = StreamableHTTPSessionManager(
            app=_mcp._mcp_server,
            event_store=None,
            json_response=True,
            stateless=True,
        )
    return _session_manager


@asynccontextmanager
async def mcp_lifespan(app):
    """FastAPI lifespan hook — starts the StreamableHTTP session manager.
    Eagerly loads the BGE reranker so the service fails fast if it can't load,
    rather than silently degrading every search to raw Qdrant scores."""
    from tools_mcp.search import _get_reranker
    if _get_reranker() is None:
        raise RuntimeError(
            "BGE reranker failed to load — refusing to start. "
            "Check `pip show sentence-transformers` and network access to huggingface.co."
        )
    sm = _get_session_manager()
    async with sm.run():
        yield


async def mcp_asgi(scope, receive, send):
    """ASGI handler for /mcp — forwards to the session manager."""
    sm = _get_session_manager()
    await sm.handle_request(scope, receive, send)


def get_sse_starlette_app():
    """Return the Starlette app with /sse and /messages routes (legacy transport)."""
    return _mcp.sse_app()
