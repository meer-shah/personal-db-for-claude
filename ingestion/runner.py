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
WORK_DIR         = Path("/var/pkp/work")
DELTA_TOKEN_FILE = Path("/var/pkp/delta_token.json")
EXCLUSIONS_FILE  = REPO_ROOT / "config" / "exclusions.txt"

# Bad-file quarantine: persistent ledger of files that have repeatedly failed
# the parser/chunker/embedder. After QUARANTINE_THRESHOLD consecutive failures
# we skip the file on subsequent runs instead of looping forever on it. The
# ledger is keyed by onedrive_item_id so renames don't matter; if the user
# re-saves the file (content_hash changes) we retry once in case they fixed
# the corruption.
QUARANTINE_FILE      = Path("/var/pkp/bad_files.json")
QUARANTINE_THRESHOLD = 3

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

# ── Bad-file quarantine ───────────────────────────────────────────────────────

# Module-level cache + lock so concurrent workers see consistent state and
# don't all hammer the disk. Loaded lazily on first access.
_quarantine_cache: dict | None = None
import threading as _threading
_quarantine_lock = _threading.Lock()


def _load_quarantine() -> dict:
    """Read the quarantine ledger from disk (or return empty if missing)."""
    global _quarantine_cache
    with _quarantine_lock:
        if _quarantine_cache is not None:
            return _quarantine_cache
        if QUARANTINE_FILE.exists():
            try:
                _quarantine_cache = json.loads(QUARANTINE_FILE.read_text())
            except Exception as e:
                log.warning("Could not parse quarantine file (%s) — starting fresh", e)
                _quarantine_cache = {}
        else:
            _quarantine_cache = {}
        return _quarantine_cache


def _save_quarantine() -> None:
    """Persist the quarantine ledger atomically (write to temp + rename)."""
    if _quarantine_cache is None:
        return
    try:
        QUARANTINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = QUARANTINE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_quarantine_cache, indent=2))
        tmp.replace(QUARANTINE_FILE)
    except Exception as e:
        log.warning("Could not persist quarantine file: %s", e)


def _is_quarantined(onedrive_item_id: str, content_hash: str | None = None) -> tuple[bool, dict | None]:
    """
    Return (skip_this_file, ledger_entry).

    skip_this_file is True if the file has hit QUARANTINE_THRESHOLD consecutive
    failures AND its content_hash matches the one we last failed on (so a
    re-saved file gets a fresh chance).
    """
    ledger = _load_quarantine()
    entry  = ledger.get(onedrive_item_id)
    if not entry:
        return (False, None)
    if entry.get("fail_count", 0) < QUARANTINE_THRESHOLD:
        return (False, entry)
    # If user re-saved the file, allow a retry.
    if content_hash and entry.get("content_hash") != content_hash:
        return (False, entry)
    return (True, entry)


def _record_failure(onedrive_item_id: str, file_name: str, content_hash: str | None, error: str) -> int:
    """Increment failure count for this file. Returns the new count."""
    with _quarantine_lock:
        ledger = _quarantine_cache if _quarantine_cache is not None else _load_quarantine()
        now    = datetime.now(tz=timezone.utc).isoformat()
        entry  = ledger.get(onedrive_item_id, {})

        # If the content_hash changed since the last failure, this is a "new"
        # version of the file — reset the count rather than carrying old failures.
        if content_hash and entry.get("content_hash") and entry["content_hash"] != content_hash:
            entry = {}

        entry.update({
            "file_name":    file_name,
            "content_hash": content_hash,
            "fail_count":   entry.get("fail_count", 0) + 1,
            "first_seen":   entry.get("first_seen", now),
            "last_attempt": now,
            "last_error":   error[:500],   # cap to avoid unbounded growth
        })
        ledger[onedrive_item_id] = entry
        _save_quarantine()
        return entry["fail_count"]


def _clear_failure(onedrive_item_id: str) -> None:
    """Remove a file from the quarantine ledger after a successful process."""
    with _quarantine_lock:
        ledger = _quarantine_cache if _quarantine_cache is not None else _load_quarantine()
        if onedrive_item_id in ledger:
            del ledger[onedrive_item_id]
            _save_quarantine()


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

    # Quarantine check — skip files that have repeatedly crashed the parser.
    # Same-hash retries are skipped fast; if the user re-saved the file
    # (different hash), _is_quarantined returns False and we try again.
    quarantined, q_entry = _is_quarantined(onedrive_item_id, file_hash)
    if quarantined and not force:
        log.warning(
            "SKIP-QUARANTINED  %s (failed %d times, last error: %s)",
            file_name, q_entry.get("fail_count", 0), (q_entry.get("last_error") or "")[:120],
        )
        return {
            "file": file_name, "status": "quarantined", "chunks": 0,
            "error": f"quarantined after {q_entry.get('fail_count', 0)} failures: {q_entry.get('last_error', '')}",
        }

    try:
        raw_chunks = _parse_file(local_path)
    except Exception as e:
        n = _record_failure(onedrive_item_id, file_name, file_hash, f"PARSE: {e}")
        log.error("PARSE ERROR  %s (failure %d/%d): %s", file_name, n, QUARANTINE_THRESHOLD, e)
        return {"file": file_name, "status": "error", "chunks": 0, "error": str(e)}

    if not raw_chunks:
        # Empty isn't a "bad file" — clear any stale failures (file may have
        # been re-saved as legitimately empty).
        _clear_failure(onedrive_item_id)
        log.info("EMPTY %s (no content extracted)", file_name)
        return {"file": file_name, "status": "empty", "chunks": 0, "error": None}

    try:
        chunked  = chunk_texts(raw_chunks)
        embedded = embed_chunks(chunked)
    except Exception as e:
        n = _record_failure(onedrive_item_id, file_name, file_hash, f"CHUNK/EMBED: {e}")
        log.error("CHUNK/EMBED ERROR  %s (failure %d/%d): %s", file_name, n, QUARANTINE_THRESHOLD, e)
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
        # Qdrant errors are typically transient (network, server restart) —
        # don't count them toward quarantine. The next run will retry.
        log.error("QDRANT ERROR  %s: %s", file_name, e)
        return {"file": file_name, "status": "error", "chunks": 0, "error": str(e)}

    # Successful end-to-end — clear any prior quarantine state for this file.
    _clear_failure(onedrive_item_id)

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

    Uses a shared TokenManager so the ~60-min Graph token TTL doesn't kill
    long runs — concurrent expiry triggers exactly one refresh thanks to
    the manager's lock + double-checked-locking pattern.

    Handles SIGINT (Ctrl+C) and SIGTERM gracefully: the enumeration loop
    breaks at the next iteration, in-flight work is cancelled, and a
    summary of completed files is written before exiting. Content-hash
    dedup makes restart safe — already-indexed files are skipped.
    """
    import signal
    import threading
    from onedrive import TokenManager

    exclusions = _load_exclusions()
    client = _get_qdrant()
    _ensure_collection(client)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    # Clean up any leftover temp files from a previous crashed run.
    for f in WORK_DIR.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass

    tm = TokenManager()

    # Graceful shutdown flag. Set by SIGINT/SIGTERM; checked in the
    # enumeration loop so Ctrl+C actually stops the indexer instead of
    # waiting for all 491k files to enumerate.
    stop_event = threading.Event()
    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM)

    def _shutdown_handler(signum, _frame):
        if not stop_event.is_set():
            log.warning(
                "Received signal %d — stopping enumeration; "
                "in-flight files will finish, queued work will be cancelled. "
                "Restart is safe (content-hash dedup will skip already-indexed files).",
                signum,
            )
            stop_event.set()
        else:
            log.warning("Second signal %d — forcing exit", signum)
            # Restore default handler so a third Ctrl+C definitely kills us.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

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
            _download_to(tm, file_id, local_path)

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
    submitted_count = 0

    # Max in-flight futures before we pause enumeration and drain.
    # Each future holds ~a few MB (downloaded bytes + chunks). 48 in-flight
    # across 12 workers = ~4 tasks queued per worker — enough to keep all
    # workers busy without accumulating the entire 491k-file list in RAM.
    # Worker count is tunable via PKP_INGEST_WORKERS. Default 12 is sized
    # for CPX62 (16 vCPU, 32 GB RAM). If RSS grows too fast in production,
    # lower this (e.g. PKP_INGEST_WORKERS=6) to halve the per-thread
    # allocator footprint inside the embedder. MAX_IN_FLIGHT scales with it
    # so the in-flight window stays at ~4× workers.
    workers = int(os.getenv("PKP_INGEST_WORKERS", "12"))
    MAX_IN_FLIGHT = workers * 4

    process_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proc")
    futures: list = []

    log.info(
        "OneDrive full (streaming): enumeration + processing run concurrently "
        "(workers=%d, max_in_flight=%d)",
        workers, MAX_IN_FLIGHT,
    )

    try:
        for item in _stream_all_files(tm):
            if stop_event.is_set():
                log.info("Stop requested — breaking enumeration loop")
                break

            seen_count += 1
            if _is_excluded(item.get("name", ""), exclusions):
                skipped_count += 1
                continue

            futures.append(process_pool.submit(_process_item, item))
            submitted_count += 1

            # Drain completed futures whenever the in-flight window fills up.
            # This keeps memory bounded — we never queue more than MAX_IN_FLIGHT
            # items ahead of the workers regardless of how fast enumeration runs.
            while len(futures) >= MAX_IN_FLIGHT:
                done_futures = [f for f in futures if f.done()]
                for f in done_futures:
                    results.append(f.result())
                    futures.remove(f)
                if not done_futures:
                    # Nothing done yet — yield the GIL briefly and retry.
                    import time as _time
                    _time.sleep(0.05)
                if stop_event.is_set():
                    break

            # Periodic progress log.
            if seen_count % 1000 == 0:
                log.info(
                    "Enumerated %d files so far (%d in-flight, %d excluded)",
                    seen_count, len(futures), skipped_count,
                )

        if not stop_event.is_set():
            log.info(
                "Enumeration complete: %d files seen, %d submitted, %d excluded",
                seen_count, submitted_count, skipped_count,
            )

        # Drain remaining in-flight futures (or those still running after stop).
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                log.error("Future failed during drain: %s", e)
    finally:
        # cancel_futures=True drops anything still queued; wait=True lets
        # currently-running tasks finish so they commit cleanly to Qdrant.
        process_pool.shutdown(wait=True, cancel_futures=True)
        # Restore previous signal handlers so this doesn't leak across runs
        # in the same process (e.g. test harnesses).
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)

    _print_summary(results, total_files=submitted_count)

# ── OneDrive delta mode ───────────────────────────────────────────────────────

def run_delta() -> None:
    """Index only files changed since last delta run."""
    from onedrive import TokenManager

    exclusions = _load_exclusions()
    client = _get_qdrant()
    _ensure_collection(client)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for f in WORK_DIR.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass

    tm    = TokenManager()
    GRAPH = "https://graph.microsoft.com/v1.0"

    delta_token = None
    if DELTA_TOKEN_FILE.exists():
        delta_token = json.loads(DELTA_TOKEN_FILE.read_text()).get("token")

    url = (
        f"{GRAPH}/me/drive/root/delta?$select=id,name,file,folder,parentReference,lastModifiedDateTime,createdDateTime,createdBy"
        if not delta_token
        else f"{GRAPH}/me/drive/root/delta(token='{delta_token}')"
    )

    session = _get_http_session()

    def _delta_get(u: str) -> dict:
        # Pull fresh auth header per request and retry once on 401.
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {tm.get()}"}
            resp = session.get(u, headers=headers, timeout=60)
            if resp.status_code == 401 and attempt == 0:
                log.warning("Graph 401 on delta paging — refreshing token")
                tm.force_refresh()
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Graph delta failed after token refresh: {u}")

    items = []
    new_token = None
    while url:
        data = _delta_get(url)
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        if "@odata.deltaLink" in data:
            delta_link = data["@odata.deltaLink"]
            new_token  = delta_link.split("token='")[1].rstrip("'") if "token='" in delta_link else None
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
            _download_to(tm, file_id, local_path)

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

    # Save delta token only after all files processed successfully.
    # Saving it before processing risks permanently skipping files if the run crashes mid-way.
    if new_token:
        DELTA_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        DELTA_TOKEN_FILE.write_text(json.dumps({"token": new_token}))

# ── HTTP session ──────────────────────────────────────────────────────────────

_http_session = None
_http_session_lock = None


def _get_http_session():
    """
    Single shared requests.Session for all Graph + download calls.

    Without this, every req_lib.get() opens a fresh TCP+TLS connection. memray
    showed urllib3.ssl_wrap_socket as the #1 allocation source (~5.4 GB,
    147M allocations) during a 5-min profile — every Graph hit was paying full
    handshake cost. A pooled Session reuses keep-alive connections per host,
    eliminating that allocation thrash.

    The pool sizes (16 connections, 32 max) are sized for our 12-worker +
    4-walker concurrency with headroom. requests.Session is thread-safe for
    concurrent .get() calls (urllib3 PoolManager is internally locked).
    """
    global _http_session, _http_session_lock
    import threading
    if _http_session_lock is None:
        _http_session_lock = threading.Lock()
    with _http_session_lock:
        if _http_session is None:
            import requests as req_lib
            from requests.adapters import HTTPAdapter
            _http_session = req_lib.Session()
            adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32)
            _http_session.mount("https://", adapter)
            _http_session.mount("http://", adapter)
    return _http_session


# ── Helpers ───────────────────────────────────────────────────────────────────

def _graph_get_with_retry(url: str, tm, max_attempts: int = 6) -> dict:
    """
    Shared Graph GET with exponential backoff for 429/5xx and network errors,
    plus 401 handling via TokenManager.force_refresh().

    `tm` is a TokenManager instance (from onedrive.py). The auth header is
    rebuilt per attempt so a refresh mid-loop takes effect immediately.
    """
    import time
    import requests as req_lib
    session = _get_http_session()
    for attempt in range(max_attempts):
        headers = {"Authorization": f"Bearer {tm.get()}"}
        try:
            resp = session.get(url, headers=headers, timeout=60)
            if resp.status_code == 401:
                log.warning(
                    "Graph 401 on %s — refreshing token (attempt %d/%d)",
                    url[:80], attempt + 1, max_attempts,
                )
                tm.force_refresh()
                continue
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


def _collect_all_files(tm) -> list[dict]:
    """
    Legacy blocking enumerator. Kept for backward compatibility; run_full()
    now uses _stream_all_files() instead so processing can start before
    enumeration finishes. Still useful for tests or short ad-hoc runs.
    Takes a TokenManager instance.
    """
    GRAPH   = "https://graph.microsoft.com/v1.0"
    results = []

    def _recurse(url: str) -> None:
        while url:
            data = _graph_get_with_retry(url, tm)
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


def _stream_all_files(tm, num_walkers: int = 4):
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

    Takes a TokenManager so long-running enumeration survives the ~60-min
    Graph access-token TTL via automatic refresh.
    """
    import queue
    import threading

    GRAPH = "https://graph.microsoft.com/v1.0"

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
                data = _graph_get_with_retry(url, tm)
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


def _download_to(tm, file_id: str, local_path: str) -> None:
    """
    Download a file from OneDrive. Takes a TokenManager so long ingest runs
    survive the ~60-min access-token TTL. Retries once on 401 by forcing
    a token refresh; other errors propagate.
    """
    GRAPH = "https://graph.microsoft.com/v1.0"
    url   = f"{GRAPH}/me/drive/items/{file_id}/content"
    session = _get_http_session()
    for attempt in range(2):
        headers = {"Authorization": f"Bearer {tm.get()}"}
        # stream=True so we don't materialize the full response body in RAM
        # before writing — important for large PDFs/PPTX in a 1TB library.
        with session.get(url, headers=headers, timeout=300, stream=True) as resp:
            if resp.status_code == 401 and attempt == 0:
                log.warning("Graph 401 on download %s — refreshing token", file_id)
                tm.force_refresh()
                continue
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            return
    raise RuntimeError(f"Download failed for {file_id} after token refresh")


def _print_summary(results: list[dict], total_files: int = 0) -> None:
    ok           = sum(1 for r in results if r["status"] == "ok")
    skipped      = sum(1 for r in results if r["status"] == "skipped")
    empty        = sum(1 for r in results if r["status"] == "empty")
    quarantined  = sum(1 for r in results if r["status"] == "quarantined")
    errors       = [r for r in results if r["status"] == "error"]
    total_chunks = sum(r["chunks"] for r in results)

    log.info("─" * 60)
    log.info(
        "Summary: %d indexed, %d skipped, %d empty, %d quarantined, %d errors",
        ok, skipped, empty, quarantined, len(errors),
    )
    log.info("Total chunks upserted: %d", total_chunks)
    if errors:
        log.warning("Failed files (this run):")
        for e in errors:
            log.warning("  %-40s %s", e["file"], e["error"])
    if quarantined:
        log.info(
            "Quarantined files skipped this run: %d (see %s for details)",
            quarantined, QUARANTINE_FILE,
        )

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
