# Sprint 2: Feature Enhancements

**Date:** May 6, 2026
**Status:** 🔄 In Progress
**Goal:** Add humanization, web search, conversation saving, and automated backups

---

## Features Overview

| # | Feature | Effort | Priority |
|---|---------|--------|----------|
| 1 | Humanize document content | System prompt only | High |
| 2 | Perplexity web search tool | New MCP tool | High |
| 3 | Conversation saver → OneDrive `/context/` | New MCP tool | Medium |
| 4 | Weekly Qdrant backup | Script + systemd timer | Medium |

---

## Feature 1: Humanize Document Content

**Approach:** Prompt engineering only — no code changes needed.

**What to do:**
Add the following to your Claude Project system prompt (in Claude.ai → Your Project → Instructions):

```
When generating content for Word documents, spreadsheets, or presentations,
write in a natural, warm, and human tone. Avoid robotic, overly formal, or
AI-sounding language. Write as a knowledgeable professional would — clear,
concise, and conversational.
```

**Why this works:** Claude writes the content before passing it to `create_word_document`. By the time `python-docx` builds the file, the text is already humanized.

**Files changed:** None

---

## Feature 2: Perplexity Web Search Tool

**Approach:** New MCP tool that calls Perplexity API. Summarizes results only if response is large (>800 tokens).

### Setup
1. Sign up at perplexity.ai → get API key (~$5 credit to start)
2. Add to `.env`:
```
PERPLEXITY_API_KEY=your_key_here
```

### New file: `tools_mcp/web_search.py`

```python
import os
import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from .auth import require_bearer

router = APIRouter()

PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
MAX_TOKENS_BEFORE_SUMMARIZE = 800

class WebSearchRequest(BaseModel):
    query: str
    summarize: bool = False  # Claude can force summarize if needed

@router.post("/tools/search_web")
async def search_web(req: WebSearchRequest, _=Depends(require_bearer)):
    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        return {"error": "Perplexity API key not configured"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "sonar",
        "messages": [{"role": "user", "content": req.query}],
        "max_tokens": 1024
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(PERPLEXITY_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    result = data["choices"][0]["message"]["content"]

    # Summarize if result is too large
    if len(result.split()) > MAX_TOKENS_BEFORE_SUMMARIZE or req.summarize:
        result = result[:3000] + "\n\n[Result truncated to preserve context window]"

    return {
        "query": req.query,
        "result": result,
        "source": "perplexity"
    }
```

### Changes to `main.py`
```python
from tools_mcp.web_search import router as web_search_router
app.include_router(web_search_router)
```

### Changes to `mcp_sse.py`
Register `search_web` as a FastMCP tool with description:
```
Use this tool when the user needs current information from the web — news,
prices, recent events, or anything not found in their OneDrive documents.
```

**Files changed:** `tools_mcp/web_search.py` (new), `main.py`, `mcp_sse.py`, `.env`

---

## Feature 3: Conversation Saver → OneDrive `/context/`

**Approach:** Claude sends conversation text/summary directly in POST body. Server saves as `.txt` and uploads to OneDrive `/context/` folder. No signed URLs needed.

### New file: `tools_mcp/save_conversation.py`

```python
import os
from datetime import datetime
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from .auth import require_bearer
from ._onedrive_upload import get_token

router = APIRouter()
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

class SaveConversationRequest(BaseModel):
    content: str           # full conversation or summary text
    filename: str = ""     # optional custom name, auto-generated if empty
    folder: str = "context"  # OneDrive folder name

@router.post("/tools/save_conversation")
async def save_conversation(req: SaveConversationRequest, _=Depends(require_bearer)):
    import httpx

    # Auto-generate filename if not provided
    filename = req.filename or f"conversation_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    if not filename.endswith(".txt"):
        filename += ".txt"

    token = await get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "text/plain"
    }

    # Upload to OneDrive /context/ folder
    upload_url = f"{GRAPH_BASE}/me/drive/root:/{req.folder}/{filename}:/content"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(upload_url, content=req.content.encode("utf-8"), headers=headers)
        resp.raise_for_status()
        data = resp.json()

    return {
        "saved": True,
        "filename": filename,
        "folder": req.folder,
        "onedrive_url": data.get("webUrl", "")
    }
```

### Changes to `main.py`
```python
from tools_mcp.save_conversation import router as save_conversation_router
app.include_router(save_conversation_router)
```

### Changes to `mcp_sse.py`
Register `save_conversation` as a FastMCP tool with description:
```
Use this tool when the user wants to save the current conversation or a summary
of it to their OneDrive. Saves as a .txt file in the /context/ folder.
Call this when user says things like "save this conversation", "save a summary",
or "save this to my OneDrive".
```

**Files changed:** `tools_mcp/save_conversation.py` (new), `main.py`, `mcp_sse.py`

---

## Feature 4: Weekly Qdrant Backup

**Approach:** Shell script snapshots Qdrant collection, compresses it, copies offserver. Systemd timer runs it weekly. Backup timestamp added to `index_status`.

### New file: `scripts/backup_qdrant.sh`

```bash
#!/bin/bash
set -e

COLLECTION="pkp_chunks"
BACKUP_DIR="/var/pkp/backups"
DATE=$(date +%Y%m%d_%H%M%S)
LOG="/var/log/pkp/backup.log"

mkdir -p "$BACKUP_DIR"

echo "[$DATE] Starting Qdrant backup..." >> "$LOG"

# Trigger snapshot
SNAPSHOT=$(curl -s -X POST "http://localhost:6333/collections/$COLLECTION/snapshots" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])")

# Wait for snapshot to be ready
sleep 5

# Copy snapshot to backup dir
cp "/qdrant/snapshots/$COLLECTION/$SNAPSHOT" "$BACKUP_DIR/${COLLECTION}_${DATE}.snapshot"

# Compress
gzip "$BACKUP_DIR/${COLLECTION}_${DATE}.snapshot"

# Keep only last 4 weekly backups (1 month)
ls -t "$BACKUP_DIR"/*.gz | tail -n +5 | xargs -r rm

# Update status file with last backup time
python3 -c "
import json, datetime
with open('/var/pkp/status.json') as f:
    s = json.load(f)
s['last_backup'] = datetime.datetime.utcnow().isoformat()
with open('/var/pkp/status.json', 'w') as f:
    json.dump(s, f)
"

echo "[$DATE] Backup complete: ${COLLECTION}_${DATE}.snapshot.gz" >> "$LOG"
```

### New file: `/etc/systemd/system/pkp-backup.service`

```ini
[Unit]
Description=PKP Qdrant Weekly Backup
After=network.target

[Service]
Type=oneshot
User=root
ExecStart=/home/marcvista/kb-app/scripts/backup_qdrant.sh
StandardOutput=append:/var/log/pkp/backup.log
StandardError=append:/var/log/pkp/backup.log
```

### New file: `/etc/systemd/system/pkp-backup.timer`

```ini
[Unit]
Description=Run PKP Qdrant backup every Sunday at 2am

[Timer]
OnCalendar=Sun *-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

### Enable the timer
```bash
sudo chmod +x /home/marcvista/kb-app/scripts/backup_qdrant.sh
sudo systemctl daemon-reload
sudo systemctl enable --now pkp-backup.timer
sudo systemctl list-timers pkp-backup.timer
```

### Optional: expose last_backup in `index_status`
In `tools_mcp/index_status.py`, the `last_backup` field from `status.json` will automatically appear in the response — no extra code needed if you already return the full status dict.

**Files changed:** `scripts/backup_qdrant.sh` (new), systemd service + timer files (new)

---

## Implementation Order

1. **Feature 1** — 5 minutes, just edit Claude Project system prompt
2. **Feature 4** — 20 minutes, no app code, just infra
3. **Feature 3** — 1 hour, straightforward upload tool
4. **Feature 2** — 1.5 hours, needs Perplexity account + API key first

**Total estimated effort:** ~3 hours

---

## Checklist

- [ ] Add humanize instructions to Claude Project system prompt
- [ ] Get Perplexity API key, add to `.env`
- [ ] Create `tools_mcp/web_search.py`
- [ ] Create `tools_mcp/save_conversation.py`
- [ ] Register both new tools in `main.py` and `mcp_sse.py`
- [ ] Create `scripts/backup_qdrant.sh`
- [ ] Create and enable `pkp-backup.service` + `pkp-backup.timer`
- [ ] Test all four features end-to-end
