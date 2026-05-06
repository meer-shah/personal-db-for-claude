import os
from pathlib import Path
import msal
import requests
from dotenv import load_dotenv, set_key

ENV_FILE = str(Path(__file__).resolve().parent / ".env")
load_dotenv(ENV_FILE)

AUTHORITY    = "https://login.microsoftonline.com/common"
SCOPE        = ["Files.ReadWrite.All"]

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
