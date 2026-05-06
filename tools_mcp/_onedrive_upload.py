"""Shared helpers for create_* tools — upload bytes to OneDrive via Graph API."""

import logging
import requests
from pathlib import Path

from fastapi import HTTPException, status

GRAPH = "https://graph.microsoft.com/v1.0"
_BLOCKED_PREFIXES = ["/System Volume Information", "/.trash", "/.Trash"]
_audit_logger = logging.getLogger("pkp.audit")


def get_token() -> str:
    from onedrive import get_access_token
    return get_access_token()


def validate_folder(folder_path: str) -> None:
    normalised = "/" + folder_path.strip("/")
    for blocked in _BLOCKED_PREFIXES:
        if normalised.lower().startswith(blocked.lower()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Writing to '{folder_path}' is not allowed",
            )


def upload_bytes(
    raw_bytes: bytes,
    filename: str,
    folder_path: str,
    mime: str,
) -> dict:
    """Upload raw bytes to OneDrive. Returns Graph API response dict."""
    validate_folder(folder_path)

    token = get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  mime,
    }
    folder = folder_path.strip("/")
    name   = filename.strip("/")
    url    = f"{GRAPH}/me/drive/root:/{folder}/{name}:/content"

    resp = requests.put(url, headers=headers, data=raw_bytes)
    resp.raise_for_status()
    data = resp.json()

    _audit_logger.info(
        "CREATE\tfilename=%r\tfolder=%r\tmime=%s\tbytes=%d\tfile_id=%s",
        filename, folder_path, mime, len(raw_bytes), data.get("id"),
    )
    return data
