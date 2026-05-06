# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Personal Knowledge Platform (PKP) — connects a 1TB OneDrive library to Claude AI via MCP. Users query their documents through the Claude.ai chat interface. Full spec in `project_files/PKP_Project_Documentation.md`.

**Deadline:** M2 demo May 8, 2026 (14-day build). Budget: $900 fixed.

## Server

| Detail | Value |
|--------|-------|
| IP | `46.225.18.94` |
| Provider | Hetzner CX52 |
| OS | Ubuntu 24.04 LTS |
| Dev user | `marcvista` |
| Project folder | `/home/marcvista/kb-app` |
| Venv | `/home/marcvista/kb-app/venv` |
| Python | `3.12.3` |
| Logs | `/var/log/pkp/` |
| Status file | `/var/pkp/status.json` |
| Secrets | `/home/marcvista/kb-app/.env` |

## Environment

All Python work uses the local venv:

```bash
source venv/bin/activate
```

All packages are installed. Key versions: `fastapi 0.136`, `uvicorn 0.46`, `qdrant-client 1.17`, `sentence-transformers 5.4`, `torch 2.11`, `msal 1.36`, `msgraph-sdk 1.56`, `python-docx`, `pdfplumber`, `openpyxl`, `python-pptx`, `tiktoken`, `python-dotenv`.

No requirements.txt — venv is the source of truth.

## Running the code

```bash
# Test OneDrive auth and file listing (device-code flow on first run)
python onedrive.py

# Start the MCP/FastAPI server (once main.py exists)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Run indexing pipeline (once runner.py exists)
python -m ingestion.runner --full

# Tests (once added)
pytest tests/
pytest tests/test_parsers.py
```

## Architecture

Two independent runtime processes:

**1. Indexing pipeline** (background daemon, `ingestion/`)
- `onedrive.py` — OAuth2 device-code auth via MSAL, Microsoft Graph API. Refresh token persisted to `.env` automatically after first auth. **Blocked**: "Allow public client flows" not yet enabled in Azure portal — waiting for Rai.
- `parsers/` — one file per type, each returns `List[dict]` with `type` and `text` keys (plus `page`/`slide`/`sheet` where applicable). Critical constraint: `.docx` must walk `doc.element.body` in order (not `doc.paragraphs`) to detect tables mid-document via `<w:tbl>` tags. **Table context rule**: every table chunk is prefixed with `[Context: <surrounding heading/title>]` so it is self-contained and searchable without the adjacent text chunk.
- `chunker.py` — 512-token max, 50-token overlap, `cl100k_base` encoding. Tables are **never split** — always their own chunk.
- `embedder.py` — `all-MiniLM-L6-v2` (384 dims), batch size 32.
- `ingestion/runner.py` — orchestrates the full pipeline. Three modes: `--local <folder>` (no OneDrive needed, for testing), `--full` (index all OneDrive files), `--delta` (incremental sync using OneDrive delta token). Change detection: sha256 of raw file bytes stored as `content_hash` in Qdrant payload; unchanged files are skipped automatically.
- Qdrant: collection `pkp_chunks`, cosine distance, binary quantization. Point ID = `uuid5(onedrive_item_id + chunk_index)` — deterministic UUID, valid for Qdrant. Upsert for idempotency. Change detection via `content_hash`.

**2. MCP server** (`tools_mcp/`, FastAPI on port 8000, Caddy proxy on 443)
- `main.py` — FastAPI entry point, registers all routers + mounts MCP transports. Start: `uvicorn main:app --host 127.0.0.1 --port 8000`
- `tools_mcp/auth.py` — `require_bearer` FastAPI dependency; validates `Authorization: Bearer <token>` against `MCP_BEARER_TOKEN` env var on every request
- `tools_mcp/search.py` — `POST /tools/search_documents`: embed query → `query_points` Qdrant top-50 → BGE reranker → top-k. Score < **0.70** → `confident=False`
- `tools_mcp/get_document.py` — `POST /tools/get_document`: scroll all chunks for a `file_path`, return sorted by `chunk_index`
- `tools_mcp/index_status.py` — `GET /tools/index_status`: reads `/var/pkp/status.json` + live Qdrant point count
- `tools_mcp/create_word.py` — `POST /tools/create_word_document`: build a real `.docx` server-side from structured input (title, sections, paragraphs, bullets, tables) via `python-docx`, then upload to OneDrive
- `tools_mcp/create_excel.py` — `POST /tools/create_spreadsheet`: build a real `.xlsx` server-side via `openpyxl` (multi-sheet, bold frozen header row, auto-sized columns), then upload to OneDrive
- `tools_mcp/create_powerpoint.py` — `POST /tools/create_presentation`: build a real `.pptx` server-side via `python-pptx` (16:9 widescreen, title slide + content slides, bullets/body/notes), then upload to OneDrive
- `tools_mcp/upload.py` — signed-URL upload for files Claude generates in its sandbox. Two-step flow:
  - `POST /tools/get_upload_url` (auth: bearer): mints a one-time HMAC-signed URL valid for 5 minutes, returned as `https://<host>/upload/<token>`
  - `POST /upload/{token}` (no auth header — token IS the credential): accepts raw file bytes, validates signature + replay-protection, forwards to OneDrive. ≤4 MiB → simple PUT; >4 MiB → chunked Graph upload session (5 MiB chunks). Hard cap 100 MiB.
- `tools_mcp/_onedrive_upload.py` — shared helpers (`get_token`, `validate_folder`, Graph constants) used by all upload paths
- `mcp_sse.py` — wraps the REST tools as FastMCP tools and exposes them at `/mcp` (Streamable HTTP) and `/sse` (legacy SSE). Used by the Windows bridge.
- **Folder name**: the local package is `tools_mcp/` (not `mcp/`) to avoid shadowing the installed `mcp` SDK package.
- Qdrant client API: use `client.query_points()` not `client.search()` (renamed in qdrant-client 1.7+)

**3. Windows stdio bridge** (`pkp_bridge.py`)
- Runs on the client's Windows laptop as a subprocess of Claude Desktop
- Speaks MCP over stdio to Claude Desktop, forwards each tool call to `https://46-225-18-94.nip.io/tools/...` with the bearer token
- Required because Claude Desktop's "Connectors" UI demands full OAuth 2.1 dynamic client registration; stdio bypasses that
- Client setup steps live in the **"Client Setup (Windows)"** section below

## Data model

`models/chunk.py` — `Chunk` dataclass fields:
- Core: `text`, `chunk_type` (`'text'|'table'`), `file_path`, `file_name`, `file_type`, `onedrive_item_id`, `content_hash`, `chunk_index`, `vector: list[float]`
- Metadata: `page_number`, `slide_number`, `sheet_name`, `author`, `modified_date`, `created_date`

## Non-negotiable implementation rules

- **Never hardcode secrets** — `.env` only (`onedrive.py` currently violates this; fix before production)
- **Tables = always own chunk**, never split across chunk boundary; always prefixed with `[Context: ...]` from surrounding heading/title
- **Chunk size: 512 tokens**, overlap: 50 tokens, tokenizer: `cl100k_base`
- **xlsx row batching**: rows grouped into ≤400-token chunks per sheet so large sheets don't get truncated by the embedding model
- **Embedding model: `all-MiniLM-L6-v2`** (384 dims)
- **Confidence threshold: 0.70**
- **Qdrant collection: `pkp_chunks`**, cosine similarity
- **Point ID**: `uuid5(onedrive_item_id + ":" + chunk_index)` — deterministic, Qdrant-compatible UUID
- **Change detection**: sha256 of raw file bytes, stored as `content_hash` in Qdrant payload; checked before download/parse

## Secrets

`.env` at repo root:
```
ONEDRIVE_CLIENT_ID=7457def9-1ae7-4993-a464-cd8cde3aa76f
ONEDRIVE_CLIENT_SECRET=<see onedrive.py — move here>
ONEDRIVE_TENANT_ID=common
ONEDRIVE_REFRESH_TOKEN=      # written automatically by onedrive.py after first auth
MCP_BEARER_TOKEN=            # generate: python3 -c "import secrets; print(secrets.token_hex(32))"
UPLOAD_SIGNING_KEY=          # generate: python3 -c "import secrets; print(secrets.token_hex(32))"
QDRANT_HOST=localhost
QDRANT_PORT=6333
PUBLIC_BASE_URL=https://46-225-18-94.nip.io   # used to construct signed upload URLs
```

## Qdrant

Running in Docker (already up on server):
```bash
docker run -d --name qdrant --restart=always \
    -p 6333:6333 -v qdrant_data:/qdrant/storage qdrant/qdrant

# Verify
curl http://localhost:6333/collections
```

## Current state (May 1, 2026) — M2 COMPLETE + M3 ENHANCEMENTS

### M2 Status (14-day build complete, Apr 27)

| Component | Status |
|-----------|--------|
| Hetzner CX52 provisioned | ✅ Done |
| UFW firewall (22 + 443 only) | ✅ Done |
| SSH key auth | ✅ Done |
| Docker + Qdrant running | ✅ Done |
| Caddy HTTPS reverse proxy | ✅ Done — `46-225-18-94.nip.io` with Let's Encrypt cert |
| Python venv + all packages | ✅ Done |
| `onedrive.py` | ✅ Done — device-code flow working, refresh token persisted |
| Azure app configured | ✅ Done — "Allow public client flows" enabled, platform configured |
| Parsers (docx/pdf/xlsx/pptx/plain) | ✅ Done |
| `chunker.py` | ✅ Done — 512 tokens max, 50-token overlap |
| `embedder.py` | ✅ Done — `all-MiniLM-L6-v2`, batch size 32 |
| `models/chunk.py` | ✅ Done |
| `ingestion/runner.py` | ✅ Done — full/delta/local modes, content hash dedup, stale chunk deletion |
| `main.py` + 7 REST tools | ✅ Done — all tools live and rate-limited (search, get_document, index_status, create_word_document, create_spreadsheet, create_presentation, get_upload_url + /upload/{token}) |
| MCP SSE/Streamable HTTP transport | ✅ Done — `/mcp` working with Claude Desktop |
| systemd services + timer | ✅ Done — `pkp-mcp.service` running, `pkp-indexer.timer` hourly |
| Windows stdio bridge | ✅ Done — tested on Windows, all tools verified |
| Claude Desktop end-to-end | ✅ **Complete — documents found, metadata correct, relevance scoring working** |
| First full OneDrive index | ✅ Done — 329 files, 11,769 chunks, 0 real errors |
| BGE reranker (fail-fast) | ✅ Done — eager init at startup, 100-candidate pool, 0.70 threshold |
| M2 acceptance criteria | ✅ **All met** — see M2_COMPLETION_SUMMARY.md |

### M3 Status (1TB-scale optimization, May 1)

| Component | Status |
|-----------|--------|
| Candidate pool: 100 → 500 | ✅ Done — **5× broader search coverage** |
| HNSW ef_construct: 100 → 256 | ✅ Done — live collection updated, new collections configured |
| Binary quantization rescoring | ✅ Done — `rescore=True, oversampling=2.0` at query time |
| Reranker batch_size: 32 → 64 | ✅ Done — ~50% faster reranking |
| Context-window expansion | ✅ Done — merge chunk ±1 neighbours for full paragraph context |
| chunk_index payload index | ✅ Done — fast Range queries for context expansion |
| top_k cap: 10 → 15 | ✅ Done — allows broader result sets when needed |
| Query expansion (Claude prompt) | ⏳ Pending — add to Claude Project system prompt (see QUERY_EXPANSION_PROMPT.md) |

### Future (R25+)

| Component | Status |
|-----------|--------|
| LUKS disk encryption | ⏳ Optional (for production deployment) |
| Qdrant off-server backups | ⏳ Recommended for production |
| Embedding model upgrade to bge-small-en-v1.5 | ⏳ Optional (quality improvement, requires re-index) |

## Client Setup (Windows)

Everything the client needs to connect Claude Desktop on their Windows machine to the live PKP server.

### Prerequisites
- Windows 10 or 11
- Python 3.10 or higher (https://python.org — tick **"Add Python to PATH"** during install)
- Claude Desktop installed (https://claude.ai/download)

### Steps

1. **Install required Python packages.** Open PowerShell and run:
   ```powershell
   pip install --user mcp httpx
   ```

2. **Save the bridge script.** Create the folder `C:\pkp\` and copy `pkp_bridge.py` (from this repo) to `C:\pkp\pkp_bridge.py`.

3. **Edit Claude Desktop's config.** Press `Win+R`, paste:
   ```
   %APPDATA%\Claude\claude_desktop_config.json
   ```
   Set the file contents to:
   ```json
   {
     "mcpServers": {
       "pkp": {
         "command": "python",
         "args": ["C:\\pkp\\pkp_bridge.py"],
         "env": {
           "PKP_SERVER_URL": "https://46-225-18-94.nip.io",
           "PKP_BEARER_TOKEN": "<bearer token — see Credentials section>"
         }
       }
     }
   }
   ```

4. **Fully quit Claude Desktop** (right-click the tray icon → **Quit**, don't just close the window) and reopen it.

5. **Verify.** In a new chat, click the **🔌 plug icon** at the bottom of the message box. You should see "pkp" with 4 tools: `search_documents`, `get_document`, `index_status`, `get_upload_url`. Try asking *"Run index_status"* to confirm.

### Troubleshooting
- "spawn python ENOENT" → Python is not on PATH. Reinstall Python with the **"Add to PATH"** option ticked, or use the full path to `python.exe` in the `command` field.
- Tools list is empty → check `%APPDATA%\Claude\logs\mcp*.log` for errors. Most common cause is a missing `pip install --user mcp httpx`.
- "401 Unauthorized" responses → bearer token mismatch with the server's `.env`.

## Credentials & Variables

> ⚠️ **Do not commit this file to a public repository.** All secrets below grant access to the live server.

### Server access
| Item | Value |
|------|-------|
| SSH | `ssh marcvista@46.225.18.94` (key auth only) |
| Sudo password | _(set on the server during provisioning — known to marcvista)_ |
| Project root | `/home/marcvista/kb-app` |

### `.env` on the server (`/home/marcvista/kb-app/.env`)
| Variable | Value | Notes |
|----------|-------|-------|
| `MCP_BEARER_TOKEN` | `_(set in server .env; never commit)_` | Same token goes into client's Claude Desktop config |
| `UPLOAD_SIGNING_KEY` | _(set in server `.env`; never leaves the server)_ | HMAC key used to sign one-time upload URLs. Rotate to invalidate all in-flight URLs. |
| `PUBLIC_BASE_URL` | `https://46-225-18-94.nip.io` | Used to construct upload URLs returned by `get_upload_url`. |
| `QDRANT_HOST` | `localhost` | |
| `QDRANT_PORT` | `6333` | |
| `ONEDRIVE_CLIENT_ID` | _(dev: using your own Azure app for testing; will switch to Rai's app at handoff)_ | Azure app client ID — swap this when handing off to Rai |
| `ONEDRIVE_CLIENT_SECRET` | _(dev: in your Azure app settings; will swap at handoff)_ | Azure app secret — swap when handing off |
| `ONEDRIVE_TENANT_ID` | `common` | |
| `ONEDRIVE_REFRESH_TOKEN` | _(written by `onedrive.py` after first device-code login)_ | Will be your own OneDrive during dev; becomes Rai's at handoff |

### Public endpoint
| Item | Value |
|------|-------|
| Base URL | `https://46-225-18-94.nip.io` |
| MCP endpoint | `https://46-225-18-94.nip.io/mcp/` |
| Health check | `https://46-225-18-94.nip.io/health` |
| Upload (signed URL) | `https://46-225-18-94.nip.io/upload/{token}` — issued by `get_upload_url`, valid 5 min, one-time use |
| TLS cert | Let's Encrypt, auto-renewed by Caddy |

### Service management
```bash
# MCP server
sudo systemctl status  pkp-mcp.service
sudo systemctl restart pkp-mcp.service
tail -f /var/log/pkp/mcp.log

# Indexer timer (runs hourly)
sudo systemctl status  pkp-indexer.timer
sudo systemctl list-timers pkp-indexer.timer

# Caddy
sudo systemctl status caddy
sudo systemctl reload caddy
```

## Items needing the user's attention

- **Azure app configuration for device-code flow** — currently using your own Azure app for development. When handing off to Rai: (1) ask him to add "Mobile and desktop applications" platform on the **Authentication (Preview)** → **Redirect URI configuration** tab, (2) verify manifest has `"allowPublicClient": true`, (3) provide him with the new `ONEDRIVE_CLIENT_ID` and `ONEDRIVE_CLIENT_SECRET` so he can update the server `.env`. See "Handoff checklist" below.
- **LUKS disk encryption (R25)** — needs scheduled downtime; not yet started.
- **Caddy log rotation** — Caddy writes to its own log; verify it's rotated by `logrotate` or systemd-journald to avoid filling disk after months of operation.
- **Bearer token rotation policy** — the current `MCP_BEARER_TOKEN` is the only auth between client and server. Decide a rotation cadence with the client (e.g. every 90 days) and document the swap procedure (server `.env` + client config update simultaneously).
- **Qdrant volume backup** — currently no off-server backup. If Hetzner volume fails, the entire index is gone (rebuildable from OneDrive but costly). Recommend `qdrant snapshot` + rclone to an off-host bucket.

