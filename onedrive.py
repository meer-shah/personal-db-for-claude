import logging
import os
import threading
import time
from pathlib import Path
import msal
import requests
from dotenv import load_dotenv, set_key

ENV_FILE = str(Path(__file__).resolve().parent / ".env")
load_dotenv(ENV_FILE)

AUTHORITY    = "https://login.microsoftonline.com/common"
SCOPE        = ["Files.ReadWrite.All"]

log = logging.getLogger("onedrive")

# ── Authentication (Device Code Flow) ────────────────────────────────────────

def get_access_token() -> str:
    """
    Authenticate via device code flow using MSAL PublicClientApplication.
    Stores the refresh token back to .env after each successful auth so
    subsequent runs are silent (no browser needed).
    """
    client_id     = os.getenv("ONEDRIVE_CLIENT_ID")
    refresh_token = os.getenv("ONEDRIVE_REFRESH_TOKEN")

    if not client_id:
        raise RuntimeError("ONEDRIVE_CLIENT_ID is not set in .env")

    app = msal.PublicClientApplication(client_id=client_id, authority=AUTHORITY)

    if refresh_token:
        result = app.acquire_token_by_refresh_token(refresh_token, scopes=SCOPE)
        if "access_token" in result:
            print("[OK] Authenticated silently using stored refresh token.")
            new_refresh = result.get("refresh_token")
            if new_refresh:
                set_key(ENV_FILE, "ONEDRIVE_REFRESH_TOKEN", new_refresh)
            return result["access_token"]
        print("[WARN] Refresh token expired or invalid. Starting device flow...")

    flow = app.initiate_device_flow(scopes=SCOPE)
    if "user_code" not in flow:
        raise RuntimeError(f"Failed to create device flow: {flow}")

    print("=" * 60)
    print(f"Open a browser and visit:\n{flow['verification_uri']}")
    print(f"Enter the code: {flow['user_code']}")
    print("=" * 60)

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(f"Authentication failed: {result.get('error_description', result)}")

    refresh = result.get("refresh_token")
    if refresh:
        set_key(ENV_FILE, "ONEDRIVE_REFRESH_TOKEN", refresh)
        print("[OK] Refresh token stored in .env")

    return result["access_token"]

# ── TokenManager: long-running access-token refresh ──────────────────────────
#
# Microsoft Graph access tokens for personal accounts last ~60-90 minutes,
# but indexing a 1 TB OneDrive library can take many hours. The original
# `get_access_token()` is fine for short scripts (it returns a single string),
# but a long ingestion run that captures one token at startup will start
# 401-ing on every request once the token expires mid-run.
#
# `TokenManager` solves that: it caches the current access token, refreshes
# it proactively before expiry, and exposes a `force_refresh()` for callers
# who hit a 401 anyway. It is thread-safe so multiple ingestion workers can
# share a single instance without dog-piling the refresh.
#
# Usage in ingestion.runner:
#     tm = TokenManager()
#     headers = {"Authorization": f"Bearer {tm.get()}"}
#     # on 401:
#     tm.force_refresh()

class TokenManager:
    """Thread-safe Graph access-token holder with automatic refresh."""

    # Refresh proactively this many seconds before the token expires.
    _REFRESH_LEEWAY_S = 5 * 60        # 5 minutes
    # Fallback expiry if MSAL doesn't report one (it usually does).
    _DEFAULT_TTL_S    = 55 * 60       # 55 minutes

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()
        self._app: msal.PublicClientApplication | None = None

    def get(self) -> str:
        """Return a valid access token, refreshing if it is near expiry."""
        if self._needs_refresh():
            with self._lock:
                # Double-checked: another thread may have refreshed while we waited.
                if self._needs_refresh():
                    self._refresh()
        return self._token  # type: ignore[return-value]

    def force_refresh(self) -> str:
        """Force a refresh now — call after a 401 from Graph."""
        with self._lock:
            self._refresh()
        return self._token  # type: ignore[return-value]

    def _needs_refresh(self) -> bool:
        return (
            self._token is None
            or time.time() >= self._expires_at - self._REFRESH_LEEWAY_S
        )

    def _refresh(self) -> None:
        # Re-load .env each refresh so a rotated refresh token written by a
        # previous refresh is picked up.
        load_dotenv(ENV_FILE, override=True)
        client_id     = os.getenv("ONEDRIVE_CLIENT_ID")
        refresh_token = os.getenv("ONEDRIVE_REFRESH_TOKEN")
        if not client_id:
            raise RuntimeError("ONEDRIVE_CLIENT_ID is not set in .env")
        if not refresh_token:
            raise RuntimeError(
                "ONEDRIVE_REFRESH_TOKEN missing — run `python onedrive.py` once "
                "to seed it via the device-code flow."
            )

        if self._app is None:
            self._app = msal.PublicClientApplication(
                client_id=client_id, authority=AUTHORITY
            )

        result = self._app.acquire_token_by_refresh_token(
            refresh_token, scopes=SCOPE
        )
        if "access_token" not in result:
            raise RuntimeError(
                f"Token refresh failed: {result.get('error_description', result)}"
            )

        self._token = result["access_token"]
        ttl = int(result.get("expires_in") or self._DEFAULT_TTL_S)
        self._expires_at = time.time() + ttl

        # Microsoft rotates the refresh token on each use — persist the new one
        # so the next process start can authenticate silently.
        new_refresh = result.get("refresh_token")
        if new_refresh and new_refresh != refresh_token:
            set_key(ENV_FILE, "ONEDRIVE_REFRESH_TOKEN", new_refresh)
        log.info("Graph access token refreshed (TTL %ds)", ttl)


# ── Graph API helpers ─────────────────────────────────────────────────────────

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def list_files(token: str, folder_path: str = "/") -> list[dict]:
    """List files and folders at the given OneDrive path (non-recursive)."""
    headers = {"Authorization": f"Bearer {token}"}
    if folder_path == "/":
        url = f"{GRAPH_BASE}/me/drive/root/children"
    else:
        clean = folder_path.strip("/")
        url   = f"{GRAPH_BASE}/me/drive/root:/{clean}:/children"

    items = []
    while url:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return items


def download_file(token: str, file_id: str, dest_path: str) -> str:
    """Download a file by item ID to dest_path. Returns dest_path."""
    headers = {"Authorization": f"Bearer {token}"}
    url  = f"{GRAPH_BASE}/me/drive/items/{file_id}/content"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    print(f"[OK] Downloaded {file_id} ({len(resp.content)} bytes) → {dest_path}")
    return dest_path

# ── Main — quick connection test ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        token = get_access_token()
        print("\n[OK] Access token obtained. Listing OneDrive root...\n")
        files = list_files(token, "/")
        print(f"Found {len(files)} items:\n")
        for item in files[:10]:
            kind = "Folder" if item.get("folder") else "File"
            print(f"  [{kind}] {item.get('name')} ({item.get('size', 0)} bytes)")
        print("\n[DONE] OneDrive connection verified.")
    except Exception as e:
        print(f"[ERROR] {e}")
