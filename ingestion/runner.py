"""
Ingestion pipeline runner.

Modes:
  --local <folder>   Index all supported files under a local directory (no OneDrive needed)
  --full             Download and index everything from OneDrive
  --delta            Download and index only files changed since last run (OneDrive delta)

Usage:
  python -m ingestion.runner --local /path/to/docs
  python -m ingestion.runner --full
  python -m ingestion.runner --delta
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FloatIndexParams,
    KeywordIndexParams,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
    BinaryQuantization,
    BinaryQuantizationConfig,
    HnswConfigDiff,
)

from chunker import chunk_texts
from embedder import embed_chunks
from models.chunk import Chunk

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

# ── Constants ─────────────────────────────────────────────────────────────────

COLLECTION       = "pkp_chunks"
VECTOR_SIZE      = 384
SUPPORTED_EXTS   = {".docx", ".pdf", ".xlsx", ".pptx", ".txt", ".md", ".csv"}
WORK_DIR         = Path("/tmp/pkp_work")
DELTA_TOKEN_FILE = Path("/var/pkp/delta_token.json")
EXCLUSIONS_FILE  = REPO_ROOT / "config" / "exclusions.txt"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("runner")

# ── Exclusion list ────────────────────────────────────────────────────────────

def _load_exclusions() -> list[str]:
    """Load glob-style exclusion patterns from config/exclusions.txt."""
    if not EXCLUSIONS_FILE.exists():
        return []
    lines = EXCLUSIONS_FILE.read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def _is_excluded(path: str, patterns: list[str]) -> bool:
    """Return True if path matches any exclusion pattern."""
    import fnmatch
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern):
            return True
        # Also match if any path component matches the pattern (folder exclusion)
        if any(fnmatch.fnmatch(part, pattern) for part in Path(path).parts):
            return True
    return False

# ── Parser router ─────────────────────────────────────────────────────────────

def _parse_file(file_path: str) -> list[dict]:
    ext = Path(file_path).suffix.lower()
    if ext == ".docx":
        from parsers.docx_parser import parse_docx
        return parse_docx(file_path)
    elif ext == ".pdf":
        from parsers.pdf_parser import parse_pdf
        return parse_pdf(file_path)
    elif ext == ".xlsx":
        from parsers.xlsx_parser import parse_xlsx
        return parse_xlsx(file_path)
    elif ext == ".pptx":
        from parsers.pptx_parser import parse_pptx
        return parse_pptx(file_path)
    elif ext in {".txt", ".md", ".csv"}:
        from parsers.plain_parser import parse_plain
        return parse_plain(file_path)
    else:
        return []

# ── Qdrant helpers ────────────────────────────────────────────────────────────

def _get_qdrant() -> QdrantClient:
    host = os.getenv("QDRANT_HOST", "localhost")
    port = int(os.getenv("QDRANT_PORT", 6333))
    return QdrantClient(host=host, port=port)


def _ensure_collection(client: QdrantClient) -> None:
    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION not in existing:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            quantization_config=BinaryQuantization(
                binary=BinaryQuantizationConfig(always_ram=True)
            ),
            # m=16 is fine; ef_construct=256 gives much better recall at 1TB scale.
            # Only takes effect on new collections — rebuild required to upgrade existing.
            hnsw_config=HnswConfigDiff(m=16, ef_construct=256),
        )
        log.info("Created Qdrant collection '%s'", COLLECTION)

    # Ensure payload indexes exist (idempotent — safe to call every run)
    _ensure_payload_indexes(client)


def _ensure_payload_indexes(client: QdrantClient) -> None:
    """Create payload indexes for fast filtering. Safe to call if they already exist."""
    info = client.get_collection(COLLECTION)
    existing = set(info.payload_schema.keys()) if info.payload_schema else set()

    keyword_fields = ["file_type", "file_path", "chunk_type", "author", "onedrive_item_id"]
    for field in keyword_fields:
        if field not in existing:
            client.create_payload_index(
                collection_name=COLLECTION,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
            log.info("Created keyword payload index: %s", field)

    # modified_date_ts stored as unix float — use float index for Range queries
    if "modified_date_ts" not in existing:
        client.create_payload_index(
            collection_name=COLLECTION,
            field_name="modified_date_ts",
            field_schema=PayloadSchemaType.FLOAT,
        )
        log.info("Created float payload index: modified_date_ts")

    # chunk_index stored as int — needed by context-window expansion (Range queries)
    if "chunk_index" not in existing:
        client.create_payload_index(
            collection_name=COLLECTION,
            field_name="chunk_index",
            field_schema=PayloadSchemaType.INTEGER,
        )
        log.info("Created integer payload index: chunk_index")


def _point_id(onedrive_item_id: str, chunk_index: int) -> str:
    raw = f"{onedrive_item_id}:{chunk_index}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _existing_hash(client: QdrantClient, onedrive_item_id: str) -> str | None:
    """Return the content_hash stored for chunk_index=0 of this item, or None."""
    point_id = _point_id(onedrive_item_id, 0)
    results = client.retrieve(
        collection_name=COLLECTION,
        ids=[point_id],
        with_payload=True,
    )
    if results:
        return results[0].payload.get("content_hash")
    return None


def _delete_item_chunks(client: QdrantClient, onedrive_item_id: str) -> None:
    """Delete all existing Qdrant points for an item before re-indexing.
    This prevents stale chunks from accumulating when a file shrinks.
    """
    client.delete(
        collection_name=COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="onedrive_item_id", match=MatchValue(value=onedrive_item_id))]
        ),
    )


def _upsert_chunks(client: QdrantClient, chunks: list[Chunk]) -> None:
    points = []
    for c in chunks:
        payload = {
            "text":               c.text,
            "chunk_type":         c.chunk_type,
            "file_path":          c.file_path,
            "file_name":          c.file_name,
            "file_type":          c.file_type,
            "onedrive_item_id":   c.onedrive_item_id,
            "content_hash":       c.content_hash,
            "chunk_index":        c.chunk_index,
            "author":             c.author,
            "page_number":        c.page_number,
            "slide_number":       c.slide_number,
            "sheet_name":         c.sheet_name,
            "modified_date":      c.modified_date.isoformat(),
            "modified_date_ts":   c.modified_date.timestamp(),   # float for Range filter
            "created_date":       c.created_date.isoformat(),
        }
        points.append(PointStruct(
            id=_point_id(c.onedrive_item_id, c.chunk_index),
            vector=c.vector,
            payload=payload,
        ))
    # Batch upsert in groups of 500
    for i in range(0, len(points), 500):
        client.upsert(collection_name=COLLECTION, points=points[i:i + 500])

# ── Core: process one file ────────────────────────────────────────────────────

def process_file(
    client: QdrantClient,
    local_path: str,
    *,
    onedrive_item_id: str,
    file_name: str,
    file_path: str,           # OneDrive/canonical path used for filtering
    modified_date: datetime,
    created_date: datetime,
    author: str | None = None,
    force: bool = False,
) -> dict:
    """
    Parse → chunk → embed → upsert one file.
    Returns a status dict: {file, status, chunks, error}
    Skips if content hash is unchanged (unless force=True).
    Deletes old chunks before upserting to prevent stale data.
    """
    file_type = Path(local_path).suffix.lstrip(".").lower()

    with open(local_path, "rb") as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()

    if not force:
        stored_hash = _existing_hash(client, onedrive_item_id)
        if stored_hash == file_hash:
            log.info("SKIP  %s (unchanged)", file_name)
            return {"file": file_name, "status": "skipped", "chunks": 0, "error": None}

    try:
        raw_chunks = _parse_file(local_path)
    except Exception as e:
        log.error("PARSE ERROR  %s: %s", file_name, e)
        return {"file": file_name, "status": "error", "chunks": 0, "error": str(e)}

    if not raw_chunks:
        log.info("EMPTY %s (no content extracted)", file_name)
        return {"file": file_name, "status": "empty", "chunks": 0, "error": None}

    try:
        chunked  = chunk_texts(raw_chunks)
        embedded = embed_chunks(chunked)
    except Exception as e:
        log.error("CHUNK/EMBED ERROR  %s: %s", file_name, e)
        return {"file": file_name, "status": "error", "chunks": 0, "error": str(e)}

    chunks: list[Chunk] = []
    for ec in embedded:
        chunks.append(Chunk(
            text             = ec["text"],
            chunk_type       = ec.get("type", "text"),
            file_path        = file_path,
            file_name        = file_name,
            file_type        = file_type,
            onedrive_item_id = onedrive_item_id,
            modified_date    = modified_date,
            created_date     = created_date,
            content_hash     = file_hash,
            chunk_index      = ec["chunk_index"],
            author           = author,
            page_number      = ec.get("page_number") or ec.get("page"),
            slide_number     = ec.get("slide_number"),
            sheet_name       = ec.get("sheet_name"),
            vector           = ec["vector"],
        ))

    try:
        # Delete old chunks first so stale points don't accumulate
        _delete_item_chunks(client, onedrive_item_id)
        _upsert_chunks(client, chunks)
    except Exception as e:
        log.error("QDRANT ERROR  %s: %s", file_name, e)
        return {"file": file_name, "status": "error", "chunks": 0, "error": str(e)}

    log.info("OK    %s → %d chunks", file_name, len(chunks))
    return {"file": file_name, "status": "ok", "chunks": len(chunks), "error": None}

# ── Local mode ────────────────────────────────────────────────────────────────

def run_local(folder: str, force: bool = False) -> None:
    folder_path = Path(folder).resolve()
    if not folder_path.is_dir():
        log.error("'%s' is not a directory", folder)
        sys.exit(1)

    exclusions = _load_exclusions()
    client = _get_qdrant()
    _ensure_collection(client)

    files = [
        p for p in folder_path.rglob("*")
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_EXTS
        and not _is_excluded(str(p), exclusions)
    ]
    total = len(files)
    log.info("Local mode: found %d supported files in '%s'", total, folder_path)

    results = []
    for fp in files:
        synthetic_id = hashlib.sha256(str(fp).encode()).hexdigest()
        stat = fp.stat()
        result = process_file(
            client,
            str(fp),
            onedrive_item_id=synthetic_id,
            file_name=fp.name,
            file_path=str(fp),
            modified_date=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            created_date=datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc),
            force=force,
        )
        results.append(result)

    _print_summary(results, total_files=total)

# ── OneDrive full mode ────────────────────────────────────────────────────────

def run_full(force: bool = False) -> None:
    """
    Streaming full-index run.

    Enumeration and processing run concurrently:
      - 4 folder-walker threads enumerate OneDrive in parallel and push files
        onto a bounded queue as they are discovered.
      - 12 worker threads pull from that queue, download/parse/embed/upsert.

    This means chunks start landing in Qdrant within minutes, instead of
    waiting hours for full enumeration to finish before any work begins.
    """
    from onedrive import get_access_token

    exclusions = _load_exclusions()
    client = _get_qdrant()
    _ensure_collection(client)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    token = get_access_token()

    def _process_item(item: dict) -> dict:
        local_path = None
        try:
            file_name  = item["name"]
            file_id    = item["id"]
            od_path    = item.get("parentReference", {}).get("path", "") + "/" + file_name
            ext        = Path(file_name).suffix.lower()
            if ext not in SUPPORTED_EXTS:
                return {"file": file_name, "status": "skipped", "chunks": 0, "error": None}

            local_path = str(WORK_DIR / f"{file_id}_{file_name}")
            _download_to(token, file_id, local_path)

            modified = datetime.fromisoformat(
                item.get("lastModifiedDateTime", datetime.now(tz=timezone.utc).isoformat())
            )
            created = datetime.fromisoformat(
                item.get("createdDateTime", modified.isoformat())
            )
            author = (item.get("createdBy") or {}).get("user", {}).get("displayName")

            return process_file(
                client,
                local_path,
                onedrive_item_id=file_id,
                file_name=file_name,
                file_path=od_path,
                modified_date=modified,
                created_date=created,
                author=author,
                force=force,
            )
        except Exception as e:
            log.error("DOWNLOAD ERROR  %s: %s", item.get("name"), e)
            return {"file": item.get("name"), "status": "error", "chunks": 0, "error": str(e)}
        finally:
            if local_path and Path(local_path).exists():
                Path(local_path).unlink()

    results = []
    seen_count = 0
    skipped_count = 0

    # 12 download/process workers. Sized for CPX62 (16 vCPU, 32 GB RAM):
    # leaves ~20 GB headroom over OS + Qdrant + embedding model + per-worker peaks.
    process_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="proc")
    futures: list = []

    log.info("OneDrive full (streaming): enumeration + processing run concurrently")

    try:
        for item in _stream_all_files(token):
            seen_count += 1
            if _is_excluded(item.get("name", ""), exclusions):
                skipped_count += 1
                continue
            futures.append(process_pool.submit(_process_item, item))

            # Periodic progress so the operator can see enumeration is alive.
            if seen_count % 1000 == 0:
                log.info(
                    "Enumerated %d files so far (%d queued, %d excluded)",
                    seen_count, len(futures), skipped_count,
                )

        log.info(
            "Enumeration complete: %d files seen, %d queued for processing, %d excluded",
            seen_count, len(futures), skipped_count,
        )

        for future in as_completed(futures):
            results.append(future.result())
    finally:
        process_pool.shutdown(wait=True)

    _print_summary(results, total_files=len(futures))

# ── OneDrive delta mode ───────────────────────────────────────────────────────

def run_delta() -> None:
    """Index only files changed since last delta run."""
    import requests as req_lib
    from onedrive import get_access_token

    exclusions = _load_exclusions()
    client = _get_qdrant()
    _ensure_collection(client)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    token   = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    GRAPH   = "https://graph.microsoft.com/v1.0"

    delta_token = None
    if DELTA_TOKEN_FILE.exists():
        delta_token = json.loads(DELTA_TOKEN_FILE.read_text()).get("token")

    url = (
        f"{GRAPH}/me/drive/root/delta?$select=id,name,file,folder,parentReference,lastModifiedDateTime,createdDateTime,createdBy"
        if not delta_token
        else f"{GRAPH}/me/drive/root/delta(token='{delta_token}')"
    )

    items = []
    while url:
        resp = req_lib.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        if "@odata.deltaLink" in data:
            delta_link = data["@odata.deltaLink"]
            new_token  = delta_link.split("token='")[1].rstrip("'") if "token='" in delta_link else None
            if new_token:
                DELTA_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                DELTA_TOKEN_FILE.write_text(json.dumps({"token": new_token}))
            break

    supported = [
        i for i in items
        if "file" in i
        and Path(i["name"]).suffix.lower() in SUPPORTED_EXTS
        and not _is_excluded(i.get("name", ""), exclusions)
    ]
    log.info("OneDrive delta: %d changed files to process", len(supported))

    results = []
    for item in supported:
        local_path = None
        try:
            file_name  = item["name"]
            file_id    = item["id"]
            od_path    = item.get("parentReference", {}).get("path", "") + "/" + file_name
            local_path = str(WORK_DIR / f"{file_id}_{file_name}")
            _download_to(token, file_id, local_path)

            modified = datetime.fromisoformat(item.get("lastModifiedDateTime", datetime.now(tz=timezone.utc).isoformat()))
            created  = datetime.fromisoformat(item.get("createdDateTime", modified.isoformat()))
            author   = (item.get("createdBy") or {}).get("user", {}).get("displayName")

            result = process_file(
                client,
                local_path,
                onedrive_item_id=file_id,
                file_name=file_name,
                file_path=od_path,
                modified_date=modified,
                created_date=created,
                author=author,
            )
            results.append(result)

        except Exception as e:
            log.error("DELTA ERROR  %s: %s", item.get("name"), e)
            results.append({"file": item.get("name"), "status": "error", "chunks": 0, "error": str(e)})
        finally:
            if local_path and Path(local_path).exists():
                Path(local_path).unlink()

    _print_summary(results, total_files=len(supported))

# ── Helpers ───────────────────────────────────────────────────────────────────

def _graph_get_with_retry(url: str, headers: dict, max_attempts: int = 6) -> dict:
    """
    Shared Graph GET with exponential backoff for 429/5xx and network errors.
    Used by both the (legacy) blocking enumerator and the streaming enumerator.
    """
    import time
    import requests as req_lib
    for attempt in range(max_attempts):
        try:
            resp = req_lib.get(url, headers=headers, timeout=60)
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning(
                    "Graph %d on %s… retry in %ds (attempt %d/%d)",
                    resp.status_code, url[:80], wait, attempt + 1, max_attempts,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except (req_lib.exceptions.ConnectionError, req_lib.exceptions.Timeout) as e:
            wait = 2 ** attempt
            log.warning(
                "Graph network error: %r — retry in %ds (attempt %d/%d)",
                e, wait, attempt + 1, max_attempts,
            )
            time.sleep(wait)
    raise RuntimeError(f"Graph API failed after {max_attempts} attempts: {url}")


def _collect_all_files(token: str) -> list[dict]:
    """
    Legacy blocking enumerator. Kept for backward compatibility; run_full()
    now uses _stream_all_files() instead so processing can start before
    enumeration finishes. Still useful for tests or short ad-hoc runs.
    """
    GRAPH   = "https://graph.microsoft.com/v1.0"
    headers = {"Authorization": f"Bearer {token}"}
    results = []

    def _recurse(url: str) -> None:
        while url:
            data = _graph_get_with_retry(url, headers)
            for item in data.get("value", []):
                if "folder" in item:
                    child_url = f"{GRAPH}/me/drive/items/{item['id']}/children"
                    _recurse(child_url)
                elif "file" in item:
                    results.append(item)
            url = data.get("@odata.nextLink")

    _recurse(
        f"{GRAPH}/me/drive/root/children"
        "?$select=id,name,file,folder,parentReference,lastModifiedDateTime,createdDateTime,createdBy"
    )
    return results


def _stream_all_files(token: str, num_walkers: int = 4):
    """
    Streaming enumerator. Walks OneDrive folders with a thread pool of
    `num_walkers` workers and yields file metadata dicts as they are
    discovered.

    Design:
      - A queue of folder URLs to walk. Initially seeded with the root.
      - Each walker pops a URL, fetches the page, pushes any subfolders
        back on the queue, and emits files via a thread-safe results queue.
      - The main thread yields from the results queue until all walkers
        are idle AND the folder queue is empty.

    Why 4 walkers (not more): Microsoft Graph throttles aggressive
    enumeration. 4 concurrent folder pages is the empirical sweet spot —
    cuts a 17-hour serial walk to ~3-4 hours without triggering 429s.
    """
    import queue
    import threading

    GRAPH   = "https://graph.microsoft.com/v1.0"
    headers = {"Authorization": f"Bearer {token}"}

    # Folders still to walk. Pagination links count as folder URLs too.
    folder_queue: "queue.Queue[str | None]" = queue.Queue()
    # Files discovered, ready to be yielded to the caller.
    file_queue: "queue.Queue[dict | object]" = queue.Queue(maxsize=2000)

    # Sentinel for "no more files coming".
    DONE = object()

    # Track in-flight folder work so we know when enumeration is truly done.
    inflight = 0
    inflight_lock = threading.Lock()
    # Set when we transition from "work pending" to "fully drained".
    drained = threading.Event()

    folder_queue.put(
        f"{GRAPH}/me/drive/root/children"
        "?$select=id,name,file,folder,parentReference,lastModifiedDateTime,createdDateTime,createdBy"
    )
    with inflight_lock:
        inflight = 1

    def _walker() -> None:
        nonlocal inflight
        while True:
            try:
                url = folder_queue.get(timeout=1.0)
            except queue.Empty:
                if drained.is_set():
                    return
                continue
            if url is None:
                folder_queue.task_done()
                return
            try:
                data = _graph_get_with_retry(url, headers)
                for item in data.get("value", []):
                    if "folder" in item:
                        child_url = (
                            f"{GRAPH}/me/drive/items/{item['id']}/children"
                            "?$select=id,name,file,folder,parentReference,lastModifiedDateTime,createdDateTime,createdBy"
                        )
                        with inflight_lock:
                            inflight += 1
                        folder_queue.put(child_url)
                    elif "file" in item:
                        file_queue.put(item)
                next_link = data.get("@odata.nextLink")
                if next_link:
                    with inflight_lock:
                        inflight += 1
                    folder_queue.put(next_link)
            except Exception as e:
                log.error("Folder walk failed for %s: %s", url[:120], e)
            finally:
                folder_queue.task_done()
                with inflight_lock:
                    inflight -= 1
                    if inflight == 0:
                        drained.set()

    walker_threads = [
        threading.Thread(target=_walker, name=f"walker-{i}", daemon=True)
        for i in range(num_walkers)
    ]
    for t in walker_threads:
        t.start()

    # Pump files out as they arrive. Stop once walkers are done AND
    # the file queue has been fully drained.
    while True:
        try:
            item = file_queue.get(timeout=1.0)
        except queue.Empty:
            if drained.is_set() and file_queue.empty():
                break
            continue
        yield item

    for t in walker_threads:
        t.join(timeout=5.0)


def _download_to(token: str, file_id: str, local_path: str) -> None:
    import requests as req_lib
    GRAPH   = "https://graph.microsoft.com/v1.0"
    headers = {"Authorization": f"Bearer {token}"}
    resp    = req_lib.get(f"{GRAPH}/me/drive/items/{file_id}/content", headers=headers)
    resp.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(resp.content)


def _print_summary(results: list[dict], total_files: int = 0) -> None:
    ok      = sum(1 for r in results if r["status"] == "ok")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    empty   = sum(1 for r in results if r["status"] == "empty")
    errors  = [r for r in results if r["status"] == "error"]
    total_chunks = sum(r["chunks"] for r in results)

    log.info("─" * 60)
    log.info("Summary: %d indexed, %d skipped, %d empty, %d errors", ok, skipped, empty, len(errors))
    log.info("Total chunks upserted: %d", total_chunks)
    if errors:
        log.warning("Failed files:")
        for e in errors:
            log.warning("  %-40s %s", e["file"], e["error"])

    _write_status(ok, errors, total_files=total_files)


def _write_status(indexed_files: int, errors: list[dict], total_files: int = 0) -> None:
    # Try /var/pkp first (production); fall back to project dir (dev without sudo)
    for candidate in (Path("/var/pkp/status.json"), REPO_ROOT / "logs" / "status.json"):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            status_path = candidate
            break
        except PermissionError:
            continue
    else:
        return  # nowhere to write
    try:
        percent = round(indexed_files / total_files * 100, 1) if total_files > 0 else 0.0
        payload = {
            "indexed_files":      indexed_files,
            "total_files":        total_files,
            "percent_complete":   percent,
            "last_run_utc":       datetime.now(tz=timezone.utc).isoformat(),
            "currently_indexing": False,
            "errors": [
                {"file": e["file"], "reason": e["error"] or "unknown"}
                for e in errors
            ],
        }
        status_path.write_text(json.dumps(payload, indent=2))
    except Exception as e:
        log.warning("Could not write status file: %s", e)

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="PKP ingestion pipeline runner")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--local", metavar="FOLDER", help="Index files from a local folder (no OneDrive)")
    group.add_argument("--full",  action="store_true", help="Full OneDrive index")
    group.add_argument("--delta", action="store_true", help="Incremental OneDrive sync (delta)")
    parser.add_argument("--force", action="store_true", help="Re-index even if content hash is unchanged")
    args = parser.parse_args()

    if args.local:
        run_local(args.local, force=args.force)
    elif args.full:
        run_full(force=args.force)
    elif args.delta:
        run_delta()


if __name__ == "__main__":
    main()
