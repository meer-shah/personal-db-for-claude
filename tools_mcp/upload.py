"""
Signed-URL upload flow for arbitrary files generated in Claude's sandbox.

Two-step protocol:

  1. Claude calls the MCP tool `get_upload_url(filename, folder_path)`.
     The server returns a one-time signed URL of the form:
       https://<host>/upload/<token>
     The token encodes (filename, folder_path, expiry) plus an HMAC signature
     so it cannot be tampered with.

  2. Claude POSTs the raw file bytes to that URL.
     The server validates the signature, marks the token used, and forwards
     the bytes to OneDrive via Microsoft Graph.

Files ≤ 4 MiB are uploaded with a single PUT.
Files > 4 MiB use a Graph upload session, streamed in 5 MiB chunks.

Security properties:
  - URLs expire after UPLOAD_TTL_SECONDS.
  - Each token can only be redeemed once (in-memory replay protection).
  - HMAC-SHA256 prevents forging or modifying tokens.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from tools_mcp.auth import require_bearer
from tools_mcp._onedrive_upload import GRAPH, get_token, validate_folder

router  = APIRouter()
limiter = Limiter(key_func=get_remote_address)

# ── Constants ────────────────────────────────────────────────────────────────

UPLOAD_TTL_SECONDS  = 5 * 60               # signed URL valid for 5 minutes
MAX_UPLOAD_BYTES    = 100 * 1024 * 1024    # 100 MiB hard ceiling
SIMPLE_PUT_LIMIT    = 4 * 1024 * 1024      # Graph simple-PUT max
SESSION_CHUNK_BYTES = 5 * 1024 * 1024      # 5 MiB chunks for upload sessions

_audit_logger = logging.getLogger("pkp.audit")

# In-memory replay protection. A redeemed token cannot be used again.
# Cleaned opportunistically on each verify.
_redeemed: dict[str, float] = {}


# ── Token mint / verify ──────────────────────────────────────────────────────

def _signing_key() -> bytes:
    key = os.getenv("UPLOAD_SIGNING_KEY", "")
    if not key:
        raise RuntimeError("UPLOAD_SIGNING_KEY not set in environment")
    return key.encode()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _mint_token(filename: str, folder_path: str) -> str:
    """Encode (filename, folder, expiry, nonce) and sign with HMAC-SHA256."""
    payload = {
        "fn":    filename,
        "fp":    folder_path,
        "exp":   int(time.time()) + UPLOAD_TTL_SECONDS,
        "nonce": _b64url_encode(os.urandom(12)),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig  = hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def _verify_token(token: str) -> dict:
    """Validate signature, expiry, and one-time use. Returns the payload dict."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed upload token")

    expected = hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url_decode(sig), expected):
        raise HTTPException(status_code=401, detail="Invalid upload token signature")

    payload = json.loads(_b64url_decode(body))

    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(status_code=401, detail="Upload token has expired")

    _gc_redeemed()
    if token in _redeemed:
        raise HTTPException(status_code=409, detail="Upload token already used")
    _redeemed[token] = time.time()

    return payload


def _gc_redeemed() -> None:
    cutoff = time.time() - UPLOAD_TTL_SECONDS - 60
    for k in [k for k, v in _redeemed.items() if v < cutoff]:
        _redeemed.pop(k, None)


# ── Graph upload helpers ─────────────────────────────────────────────────────

def _graph_simple_put(raw: bytes, filename: str, folder: str, mime: str) -> dict:
    """Single PUT — used for files ≤ 4 MiB."""
    url = f"{GRAPH}/me/drive/root:/{folder}/{filename}:/content"
    headers = {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type":  mime or "application/octet-stream",
    }
    resp = requests.put(url, headers=headers, data=raw)
    resp.raise_for_status()
    return resp.json()


def _graph_upload_session(raw: bytes, filename: str, folder: str) -> dict:
    """Resumable upload — used for files > 4 MiB. Streams 5 MiB chunks."""
    token = get_token()

    create_url = f"{GRAPH}/me/drive/root:/{folder}/{filename}:/createUploadSession"
    resp = requests.post(
        create_url,
        headers={"Authorization": f"Bearer {token}"},
        json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
    )
    resp.raise_for_status()
    upload_url = resp.json()["uploadUrl"]

    total  = len(raw)
    offset = 0
    final_response: dict | None = None

    while offset < total:
        end   = min(offset + SESSION_CHUNK_BYTES, total) - 1
        chunk = raw[offset : end + 1]
        r = requests.put(
            upload_url,
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range":  f"bytes {offset}-{end}/{total}",
            },
            data=chunk,
        )
        if r.status_code in (200, 201):
            final_response = r.json()
            break
        if r.status_code != 202:
            r.raise_for_status()
        offset = end + 1

    if final_response is None:
        raise RuntimeError("Upload session did not return a final response")
    return final_response


# ── Endpoints ────────────────────────────────────────────────────────────────

class GetUploadUrlRequest(BaseModel):
    filename:    str
    folder_path: str


class GetUploadUrlResponse(BaseModel):
    upload_url:   str
    expires_in:   int
    instructions: str


@router.post("/tools/get_upload_url", response_model=GetUploadUrlResponse)
@limiter.limit("30/minute")
def get_upload_url(
    request: Request,
    req: GetUploadUrlRequest,
    _: None = Depends(require_bearer),
) -> GetUploadUrlResponse:
    """Mint a one-time signed URL the caller can POST a file to."""
    validate_folder(req.folder_path)

    if not req.filename or "/" in req.filename or "\\" in req.filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    token = _mint_token(req.filename, req.folder_path)
    base  = os.getenv("PUBLIC_BASE_URL", "https://46-225-18-94.nip.io").rstrip("/")
    url   = f"{base}/upload/{token}"

    return GetUploadUrlResponse(
        upload_url   = url,
        expires_in   = UPLOAD_TTL_SECONDS,
        instructions = (
            "POST the raw file bytes to upload_url within "
            f"{UPLOAD_TTL_SECONDS} seconds. No Authorization header is needed. "
            "Set Content-Type to the correct MIME type for the file."
        ),
    )


@router.post("/upload/{token}")
@limiter.limit("20/minute")
async def upload_file(token: str, request: Request) -> JSONResponse:
    """Accept raw file bytes for a signed token and forward to OneDrive."""
    payload  = _verify_token(token)
    filename = payload["fn"]
    folder   = payload["fp"].strip("/")
    mime     = request.headers.get("content-type", "application/octet-stream")

    # Reject oversized requests early using Content-Length when present
    declared = request.headers.get("content-length")
    if declared and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MiB)",
        )

    # Stream the body and enforce the size cap as we read
    chunks: list[bytes] = []
    running = 0
    async for chunk in request.stream():
        running += len(chunk)
        if running > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large")
        chunks.append(chunk)
    body = b"".join(chunks)

    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    try:
        if len(body) <= SIMPLE_PUT_LIMIT:
            data = _graph_simple_put(body, filename, folder, mime)
        else:
            data = _graph_upload_session(body, filename, folder)
    except requests.HTTPError as e:
        return JSONResponse(
            status_code=502,
            content={"success": False, "error": f"OneDrive upload failed: {e}"},
        )

    _audit_logger.info(
        "UPLOAD\tfilename=%r\tfolder=%r\tmime=%s\tbytes=%d\tfile_id=%s",
        filename, folder, mime, len(body), data.get("id"),
    )

    return JSONResponse(content={
        "success":      True,
        "onedrive_url": data.get("webUrl"),
        "file_id":      data.get("id"),
        "bytes":        len(body),
    })
