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
import re
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
from parsers.docx_parser import parse_docx
from parsers.pdf_parser import parse_pdf
from parsers.xlsx_parser import parse_xlsx
from parsers.pptx_parser import parse_pptx
from parsers.plain_parser import parse_plain

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

# Priority indexing: a user can designate one or more OneDrive folders to be
# indexed FIRST, before the rest of the library. This lets them test searches
# against real content within minutes instead of waiting for a full library
# walk. State is persisted to /var/pkp/priority.json so the systemd-managed
# indexer transparently resumes priority work across crashes/restarts. After
# all priority folders complete, the file is deleted and the indexer falls
# through to a normal full sweep of the rest of the library.
PRIORITY_FILE              = Path("/var/pkp/priority.json")
MAX_PRIORITY_FOLDERS       = 10
# After IN_PROGRESS_CRASH_THRESHOLD restarts during which a specific file was
# in-progress without making progress on the run as a whole, that file is
# added to the regular bad-files quarantine ledger and skipped permanently.
# This catches OOM-on-load and similar non-exception crashes that the regular
# try/except quarantine path can't see.
IN_PROGRESS_CRASH_THRESHOLD = 3
# Soft warning threshold for big priority folders. Above this we tell the
# user phase 1 will take a while, but we don't refuse the request.
PRIORITY_SIZE_WARNING       = 5000

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("runner")


def _parse_graph_dt(value):
    """Parse a Microsoft Graph datetime string into an aware UTC datetime.

    Graph returns timestamps like '2023-11-26T09:01:20Z' and sometimes with
    7-digit fractional seconds ('...20.1234567Z'), which datetime.fromisoformat
    rejects. We normalize trailing 'Z' to '+00:00' and truncate over-long
    fractional seconds to the 6 digits Python accepts. Returns None if the
    value is missing or unparseable.
    """
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(.*\.\d{6})\d+([+-]\d{2}:\d{2})$", s)
    if m:
        s = m.group(1) + m.group(2)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _same_instant(a, b) -> bool:
    """True if two Graph/ISO datetime strings refer to the same instant.

    Robust to format differences (e.g. 'Z' vs '+00:00') that previously made
    the pre-download mtime dedup never match, forcing a full re-download of
    already-indexed files on every (re)start.
    """
    da = _parse_graph_dt(a)
    db = _parse_graph_dt(b)
    return da is not None and db is not None and da == db

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
        return parse_docx(file_path)
    elif ext == ".pdf":
        return parse_pdf(file_path)
    elif ext == ".xlsx":
        return parse_xlsx(file_path)
    elif ext == ".pptx":
        return parse_pptx(file_path)
    elif ext in {".txt", ".md", ".csv"}:
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
    """Return the content_hash stored for chunk_index=0 of this item, or None.

    NOTE: only returns the hash if the file's chunks are *fully committed* —
    i.e. all batches landed. Half-indexed files (large PDFs that crashed
    between Qdrant batch upserts) return None so they get reprocessed.
    See _upsert_chunks for the commit-marker write order.
    """
    point_id = _point_id(onedrive_item_id, 0)
    results = client.retrieve(
        collection_name=COLLECTION,
        ids=[point_id],
        with_payload=True,
    )
    if not results:
        return None
    payload = results[0].payload or {}
    # Half-indexed file detection. The `committed` flag is written by
    # _upsert_chunks as the FINAL step (chunk 0 only). Three cases:
    #   - committed=True   → fully indexed, safe to dedup
    #   - committed=False  → crash between batches, must reprocess
    #   - field missing    → legacy chunk written before commit-marker was
    #                        introduced; treat as committed for backwards
    #                        compatibility (otherwise the entire existing
    #                        20M-chunk library would re-index on first run
    #                        after the upgrade).
    if payload.get("committed") is False:
        return None
    return payload.get("content_hash")


def _existing_mtime(client: QdrantClient, onedrive_item_id: str) -> str | None:
    """Return the stored ISO modified_date of a fully-committed item, or None.

    Used as a fast pre-download dedup: if Qdrant already has chunks for this
    item AND the Graph-reported lastModifiedDateTime matches, we can skip the
    download entirely (saving bandwidth + the per-file RAM spike that was
    triggering hourly OOMs even during dedup-skip cycles).

    Only matches *committed* files — half-indexed files return None so they
    get reprocessed.
    """
    point_id = _point_id(onedrive_item_id, 0)
    results = client.retrieve(
        collection_name=COLLECTION,
        ids=[point_id],
        with_payload=True,
    )
    if not results:
        return None
    payload = results[0].payload or {}
    # Same backwards-compat rule as _existing_hash: missing flag = legacy
    # chunk, treat as committed. Only an explicit False (set by the new
    # write path) means "half-indexed, don't dedup."
    if payload.get("committed") is False:
        return None
    return payload.get("modified_date")


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
    """
    Upsert all chunks for one file, then write a 'committed=True' marker on
    chunk 0 as the final step.

    Crash-safety: for files with >500 chunks the upsert is split into multiple
    Qdrant HTTP batches. If we crashed between batches without this scheme,
    chunk 0 would already be tagged with the correct content_hash and the
    next-run dedup check would skip the file — leaving the tail chunks
    permanently missing.

    By writing all chunks with committed=False first, and only flipping
    chunk 0 to committed=True after every other batch lands, _existing_hash
    can detect half-indexed files and trigger a reprocess.
    """
    if not chunks:
        return

    points_by_index: dict[int, PointStruct] = {}
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
            "modified_date_ts":   c.modified_date.timestamp(),
            "created_date":       c.created_date.isoformat(),
            # Commit marker — flipped to True on chunk 0 only after all
            # other chunks are written. Half-indexed files have this False
            # (or missing) on chunk 0 and are correctly reprocessed.
            "committed":          False,
        }
        points_by_index[c.chunk_index] = PointStruct(
            id=_point_id(c.onedrive_item_id, c.chunk_index),
            vector=c.vector,
            payload=payload,
        )

    # Write chunk 0 LAST with committed=True; write everything else first.
    chunk0 = points_by_index.pop(0, None)
    rest = list(points_by_index.values())

    # Batch upsert the tail (non-chunk-0) in groups of 500.
    for i in range(0, len(rest), 500):
        client.upsert(collection_name=COLLECTION, points=rest[i:i + 500])

    # Finally write chunk 0 with the commit marker. If we crash before this
    # call, the next run sees committed=False (or no chunk 0 at all) and
    # reprocesses the file. If chunk 0 was the only chunk, just write it now.
    if chunk0 is not None:
        chunk0.payload["committed"] = True
        client.upsert(collection_name=COLLECTION, points=[chunk0])

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
    # Load lazily WITHOUT the lock — _load_quarantine acquires it internally.
    # Calling it inside `with _quarantine_lock` would deadlock since
    # threading.Lock is not reentrant.
    if _quarantine_cache is None:
        _load_quarantine()
    with _quarantine_lock:
        # Don't use `or {}` here — that creates a new dict if the cache is
        # an empty dict, and writes go to the throwaway dict instead of
        # the persisted cache. Reference _quarantine_cache directly.
        ledger = _quarantine_cache
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
    # See _record_failure: load outside the lock to avoid re-entry.
    if _quarantine_cache is None:
        _load_quarantine()
    with _quarantine_lock:
        ledger = _quarantine_cache
        if onedrive_item_id in ledger:
            del ledger[onedrive_item_id]
            _save_quarantine()


# ── Priority indexing ─────────────────────────────────────────────────────────

# Module-level cache + lock. The priority file is read once at startup and
# updated incrementally; persisting after every file would thrash disk.
_priority_cache: dict | None = None
_priority_lock = _threading.Lock()


def _load_priority() -> dict:
    """Read the priority ledger. Returns {} if no priority is set."""
    global _priority_cache
    with _priority_lock:
        if _priority_cache is not None:
            return _priority_cache
        if PRIORITY_FILE.exists():
            try:
                _priority_cache = json.loads(PRIORITY_FILE.read_text())
            except Exception as e:
                log.warning("Could not parse priority file (%s) — ignoring", e)
                _priority_cache = {}
        else:
            _priority_cache = {}
        return _priority_cache


def _save_priority() -> None:
    """Persist the priority ledger atomically (write-temp + rename)."""
    if _priority_cache is None:
        return
    try:
        PRIORITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PRIORITY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_priority_cache, indent=2))
        tmp.replace(PRIORITY_FILE)
    except Exception as e:
        log.warning("Could not persist priority file: %s", e)


def _clear_priority_file() -> None:
    """Remove the priority ledger entirely (priority phase complete)."""
    global _priority_cache
    with _priority_lock:
        _priority_cache = {}
        try:
            if PRIORITY_FILE.exists():
                PRIORITY_FILE.unlink()
        except Exception as e:
            log.warning("Could not delete priority file: %s", e)


def _normalize_priority_folders(folders: list[str]) -> list[str]:
    """
    Clean up user-supplied priority folders:
      - strip whitespace
      - require leading '/'
      - drop duplicates
      - drop child folders whose parent is already in the list (parent
        already covers them recursively, so re-walking the child wastes
        Graph API calls)
      - cap at MAX_PRIORITY_FOLDERS
    """
    cleaned: list[str] = []
    for f in folders:
        f = f.strip().rstrip("/")
        if not f:
            continue
        if not f.startswith("/"):
            raise ValueError(f"Priority folder must start with '/': {f!r}")
        if f not in cleaned:
            cleaned.append(f)

    # Sort by depth ascending; for each folder, drop it if any earlier folder
    # is a parent prefix of it.
    cleaned.sort(key=lambda p: p.count("/"))
    deduped: list[str] = []
    for f in cleaned:
        if any(f == p or f.startswith(p + "/") for p in deduped):
            continue
        deduped.append(f)

    if len(deduped) > MAX_PRIORITY_FOLDERS:
        raise ValueError(
            f"Too many priority folders ({len(deduped)}); max is {MAX_PRIORITY_FOLDERS}"
        )
    return deduped


def _record_in_progress(file_id: str) -> None:
    """Mark a file as currently being processed (writes to priority ledger)."""
    # Load outside the lock — _load_priority acquires it internally and
    # threading.Lock is not reentrant.
    if _priority_cache is None:
        _load_priority()
    with _priority_lock:
        # Reference _priority_cache directly. `... or {}` would create a
        # throwaway dict when the cache is an empty dict, dropping our
        # writes on the floor.
        if not _priority_cache:  # priority not active — nothing to track
            return
        in_progress = _priority_cache.setdefault("in_progress_file_ids", [])
        if file_id not in in_progress:
            in_progress.append(file_id)
            _save_priority()


def _record_done(file_id: str) -> None:
    """
    Mark a file as no longer in-progress and bump files_done.

    Always persists on every call. With ~5K priority files and ~10s per
    file across 12 workers, that's ~3 saves/sec — trivial I/O on SSD,
    and the alternative (batched writes) creates a window where a crash
    after the file finished but before the save would falsely mark the
    file as "in-progress at crash" and penalize it during crash recovery.
    Atomic write-to-tmp + rename ensures the ledger is always coherent
    on disk regardless of when we die.
    """
    if _priority_cache is None:
        _load_priority()
    with _priority_lock:
        if not _priority_cache:
            return
        in_progress = _priority_cache.get("in_progress_file_ids", [])
        if file_id in in_progress:
            in_progress.remove(file_id)
        _priority_cache["files_done"] = _priority_cache.get("files_done", 0) + 1
        _priority_cache["last_progress_utc"] = datetime.now(tz=timezone.utc).isoformat()
        _save_priority()


def _handle_crash_recovery() -> None:
    """
    On indexer startup, examine the priority ledger from the previous run.

    If the previous run made no progress AND there were files in-progress
    when it died, those files are suspect (OOM-on-load, segfault during
    parse, etc.). Bump a per-file crash count for each. Files that hit
    IN_PROGRESS_CRASH_THRESHOLD get added to the bad-files quarantine
    ledger so they're permanently skipped on subsequent attempts.

    This is the "priority-phase safety net" beyond the regular per-file
    try/except quarantine, which only catches Python-level exceptions.
    """
    if _priority_cache is None:
        _load_priority()
    with _priority_lock:
        if not _priority_cache:
            return
        ledger = _priority_cache  # alias for readability; same dict reference

        previous_in_progress = ledger.get("in_progress_file_ids", [])
        # On the very first restart, there's no prior snapshot to compare
        # against. Use None to signal "no recorded prior attempt" — we then
        # treat the existence of in-progress files at startup as evidence
        # of a crash (otherwise we'd never penalize files crashed during
        # the first ever attempt).
        files_done_before    = ledger.get("files_done_at_start_of_last_attempt")
        files_done_now       = ledger.get("files_done", 0)

        ledger["restart_count"] = ledger.get("restart_count", 0) + 1
        log.info(
            "Priority phase resuming (attempt #%d, %d files already done)",
            ledger["restart_count"], files_done_now,
        )

        if previous_in_progress:
            # Previous run died while these files were in flight. We treat
            # this as "no progress" (and penalize the files) when either:
            #   • there's no prior snapshot (first restart) AND files_done
            #     is also zero — nothing ever completed before the crash
            #   • the snapshot equals files_done_now (no completions
            #     happened between the previous attempt and now)
            # If files_done > 0 with no snapshot, the run did make progress
            # before dying, so the in-flight files were just unlucky and
            # shouldn't be penalized.
            if files_done_before is None:
                no_progress = (files_done_now == 0)
            else:
                no_progress = (files_done_before == files_done_now)
            if no_progress:
                log.warning(
                    "Previous priority attempt made no progress — incrementing "
                    "crash_count for %d in-progress files", len(previous_in_progress),
                )
                in_progress_crashes = ledger.setdefault("in_progress_crashes", {})
                for fid in previous_in_progress:
                    in_progress_crashes[fid] = in_progress_crashes.get(fid, 0) + 1
                    if in_progress_crashes[fid] >= IN_PROGRESS_CRASH_THRESHOLD:
                        # Promote to permanent quarantine. We push the
                        # quarantine fail_count straight to QUARANTINE_THRESHOLD
                        # so process_file's _is_quarantined check skips this
                        # file on its very next encounter.
                        log.error(
                            "File %s crashed indexer %d times mid-process — quarantining",
                            fid, in_progress_crashes[fid],
                        )
                        for _ in range(QUARANTINE_THRESHOLD):
                            _record_failure(
                                fid, file_name=fid, content_hash=None,
                                error=f"crashed indexer {in_progress_crashes[fid]} times mid-process",
                            )
            else:
                log.info(
                    "Previous priority attempt died with %d files in flight, but "
                    "progress was made — not penalizing them",
                    len(previous_in_progress),
                )

        # Reset in-progress and snapshot files_done for the next attempt's
        # comparison.
        ledger["in_progress_file_ids"] = []
        ledger["files_done_at_start_of_last_attempt"] = files_done_now
        _save_priority()


def _is_in_priority(od_path: str, priority_folders: list[str]) -> bool:
    """True if od_path falls under any priority folder (prefix match)."""
    od_path_norm = od_path.rstrip("/")
    for p in priority_folders:
        if od_path_norm == p or od_path_norm.startswith(p + "/"):
            return True
    return False


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

    # Stream-hash in 1 MiB blocks instead of f.read() — for a 1TB library
    # with many large PDFs/PPTX, slurping the whole file into Python's heap
    # before hashing caused per-file RAM spikes proportional to file size,
    # which compounded into the hourly OOM cycles the client was hitting
    # even during dedup-skip phases (because every file is hashed, even
    # ones that end up skipped).
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    file_hash = h.hexdigest()

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

    Priority indexing: if /var/pkp/priority.json exists (created by
    `--set-priority`), the indexer first walks just those folders ("phase 1"),
    drains them to completion, then walks the rest of the library
    excluding the priority folders ("phase 2"). This lets users test the
    system on real content within minutes instead of waiting for the full
    library walk. Priority state survives crashes — content-hash dedup
    skips files already indexed in previous attempts.
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

    # Check for active priority work BEFORE starting Phase 1. If the
    # ledger is non-empty, this is a resume — possibly after a crash —
    # so handle in-progress crash bookkeeping first.
    priority_ledger     = _load_priority()
    priority_folders    = priority_ledger.get("folders", []) if priority_ledger else []
    has_priority        = bool(priority_folders)
    if has_priority:
        _handle_crash_recovery()
        log.info(
            "Priority indexing active — Phase 1 will walk %d folder(s) first: %s",
            len(priority_folders), priority_folders,
        )

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

    workers = int(os.getenv("PKP_INGEST_WORKERS", "12"))
    MAX_IN_FLIGHT = workers * 4

    def _process_item(item: dict, *, track_progress: bool) -> dict:
        """
        Download → parse → chunk → embed → upsert one item.

        track_progress=True only during priority phase 1: each file is
        marked in-progress before processing and cleared after, so that
        on a crash mid-process we know which files were in flight (and
        can blame them via _handle_crash_recovery on next startup).
        """
        local_path = None
        file_id    = item.get("id", "")
        try:
            if track_progress and file_id:
                _record_in_progress(file_id)

            file_name  = item["name"]
            od_path    = item.get("parentReference", {}).get("path", "") + "/" + file_name
            ext        = Path(file_name).suffix.lower()
            if ext not in SUPPORTED_EXTS:
                return {"file": file_name, "status": "skipped", "chunks": 0, "error": None}

            # Fast pre-download dedup: if Qdrant already has a fully-committed
            # copy of this item at the same modified_date, skip the download
            # entirely. This eliminates the per-file RAM spike and Graph
            # download cost on restart-resume runs (where most files are
            # already indexed). Falls through to the slower hash-based check
            # in process_file() if mtime differs or chunk 0 isn't committed.
            if not force:
                item_mtime = item.get("lastModifiedDateTime")
                if item_mtime:
                    stored_mtime = _existing_mtime(client, file_id)
                    if stored_mtime and _same_instant(stored_mtime, item_mtime):
                        return {"file": file_name, "status": "skipped", "chunks": 0, "error": None}

            local_path = str(WORK_DIR / f"{file_id}_{file_name}")
            _download_to(tm, file_id, local_path)

            modified = _parse_graph_dt(item.get("lastModifiedDateTime")) or datetime.now(tz=timezone.utc)
            created = _parse_graph_dt(item.get("createdDateTime")) or modified
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
            if track_progress and file_id:
                _record_done(file_id)
            if local_path and Path(local_path).exists():
                Path(local_path).unlink()

    def _run_one_phase(
        *,
        phase_label: str,
        start_urls: list[str] | None,
        exclude_path_prefixes: list[str] | None,
        track_progress: bool,
    ) -> tuple[list[dict], int]:
        """
        Run one streaming enumeration → processing pass.

        Returns (results, submitted_count) so callers can build a summary.
        Respects stop_event for graceful shutdown.
        """
        results: list[dict] = []
        seen_count      = 0
        skipped_count   = 0
        submitted_count = 0

        process_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"proc-{phase_label}")
        futures: list = []

        log.info(
            "%s starting (workers=%d, max_in_flight=%d, exclusions=%s)",
            phase_label, workers, MAX_IN_FLIGHT,
            (exclude_path_prefixes or "none"),
        )

        try:
            for item in _stream_all_files(
                tm,
                start_urls=start_urls,
                exclude_path_prefixes=exclude_path_prefixes,
            ):
                if stop_event.is_set():
                    log.info("Stop requested — breaking %s enumeration", phase_label)
                    break

                seen_count += 1
                if _is_excluded(item.get("name", ""), exclusions):
                    skipped_count += 1
                    continue

                futures.append(process_pool.submit(_process_item, item, track_progress=track_progress))
                submitted_count += 1

                # Bounded in-flight to keep memory flat.
                while len(futures) >= MAX_IN_FLIGHT:
                    done_futures = [f for f in futures if f.done()]
                    for f in done_futures:
                        results.append(f.result())
                        futures.remove(f)
                    if not done_futures:
                        import time as _time
                        _time.sleep(0.05)
                    if stop_event.is_set():
                        break

                if seen_count % 1000 == 0:
                    log.info(
                        "%s: enumerated %d (%d in-flight, %d excluded)",
                        phase_label, seen_count, len(futures), skipped_count,
                    )

            if not stop_event.is_set():
                log.info(
                    "%s enumeration complete: %d seen, %d submitted, %d excluded",
                    phase_label, seen_count, submitted_count, skipped_count,
                )

            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    log.error("Future failed during drain: %s", e)
        finally:
            process_pool.shutdown(wait=True, cancel_futures=True)

        return results, submitted_count

    all_results: list[dict] = []
    total_submitted = 0

    try:
        # ── Phase 1: priority folders (if any) ────────────────────────────
        if has_priority:
            phase1_start_urls: list[str] = []
            for folder in priority_folders:
                url = _resolve_path_to_folder_url(tm, folder)
                if url:
                    phase1_start_urls.append(url)
                else:
                    log.error(
                        "Priority folder %r unreachable — skipping it. "
                        "Other priority folders and the full sweep will continue.",
                        folder,
                    )

            if phase1_start_urls:
                p1_results, p1_submitted = _run_one_phase(
                    phase_label="Phase 1 (priority)",
                    start_urls=phase1_start_urls,
                    exclude_path_prefixes=None,
                    track_progress=True,
                )
                all_results.extend(p1_results)
                total_submitted += p1_submitted

                if not stop_event.is_set():
                    log.info(
                        "Phase 1 complete — priority folders indexed. "
                        "System is searchable now. Continuing with full library...",
                    )
                    _clear_priority_file()
                    has_priority = False  # phase 2 should not exclude anymore

        # ── Phase 2: rest of the library (with priority folders excluded
        # if we just finished phase 1; or full library if no priority) ────
        if not stop_event.is_set():
            # If priority just completed, exclude those folders so we don't
            # re-walk them (their files would dedup-skip anyway, but skipping
            # the enumeration saves Graph API calls).
            phase2_exclude = priority_folders if priority_folders else None
            phase_label    = "Phase 2 (full library)" if priority_folders else "Full library"
            p2_results, p2_submitted = _run_one_phase(
                phase_label=phase_label,
                start_urls=None,
                exclude_path_prefixes=phase2_exclude,
                track_progress=False,
            )
            all_results.extend(p2_results)
            total_submitted += p2_submitted
    finally:
        # Restore previous signal handlers so this doesn't leak across runs.
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)

    _print_summary(all_results, total_files=total_submitted)

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

            modified = _parse_graph_dt(item.get("lastModifiedDateTime")) or datetime.now(tz=timezone.utc)
            created  = _parse_graph_dt(item.get("createdDateTime")) or modified
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


def _resolve_path_to_folder_url(tm, path: str) -> str | None:
    """
    Resolve a OneDrive path like '/Documents/fyp' to a children-listing URL
    `/me/drive/items/{id}/children?...`. Returns None if the path doesn't
    exist or isn't a folder. Used by priority indexing to start walking at
    specific folders rather than the drive root.
    """
    GRAPH   = "https://graph.microsoft.com/v1.0"
    encoded = path.lstrip("/")  # Graph wants the path without a leading slash
    # The :/ syntax tells Graph "treat this as a path under root".
    url = f"{GRAPH}/me/drive/root:/{encoded}?$select=id,folder,name"
    try:
        meta = _graph_get_with_retry(url, tm, max_attempts=3)
    except Exception as e:
        log.error("Could not resolve priority folder %r: %s", path, e)
        return None
    if "folder" not in meta:
        log.error("Priority path %r is not a folder", path)
        return None
    return (
        f"{GRAPH}/me/drive/items/{meta['id']}/children"
        "?$select=id,name,file,folder,parentReference,lastModifiedDateTime,createdDateTime,createdBy"
    )


def _stream_all_files(
    tm,
    num_walkers: int = 4,
    *,
    start_urls: list[str] | None = None,
    exclude_path_prefixes: list[str] | None = None,
):
    """
    Streaming enumerator. Walks OneDrive folders with a thread pool of
    `num_walkers` workers and yields file metadata dicts as they are
    discovered.

    Design:
      - A queue of folder URLs to walk. Initially seeded with start_urls
        (or the drive root if not provided).
      - Each walker pops a URL, fetches the page, pushes any subfolders
        back on the queue, and emits files via a thread-safe results queue.
      - The main thread yields from the results queue until all walkers
        are idle AND the folder queue is empty.

    Why 4 walkers (not more): Microsoft Graph throttles aggressive
    enumeration. 4 concurrent folder pages is the empirical sweet spot —
    cuts a 17-hour serial walk to ~3-4 hours without triggering 429s.

    Takes a TokenManager so long-running enumeration survives the ~60-min
    Graph access-token TTL via automatic refresh.

    Args:
        start_urls: explicit list of children-listing URLs to start from.
            If None, walks the whole drive starting at root. Used by
            priority indexing to walk specific folders only.
        exclude_path_prefixes: when set, files whose parentReference.path
            starts with any of these prefixes are silently dropped before
            being yielded. Used by phase-2 full sweep to skip folders
            already covered in phase 1.
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

    if start_urls:
        for u in start_urls:
            folder_queue.put(u)
        with inflight_lock:
            inflight = len(start_urls)
    else:
        folder_queue.put(
            f"{GRAPH}/me/drive/root/children"
            "?$select=id,name,file,folder,parentReference,lastModifiedDateTime,createdDateTime,createdBy"
        )
        with inflight_lock:
            inflight = 1

    def _excluded(item: dict) -> bool:
        if not exclude_path_prefixes:
            return False
        parent_path = (item.get("parentReference") or {}).get("path", "")
        # Graph returns parent paths like "/drive/root:/Documents/fyp" — strip
        # the "/drive/root:" prefix so our path comparisons match user-facing
        # paths like "/Documents/fyp".
        if parent_path.startswith("/drive/root:"):
            parent_path = parent_path[len("/drive/root:"):]
        for p in exclude_path_prefixes:
            if parent_path == p or parent_path.startswith(p + "/"):
                return True
        return False

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
                        if _excluded(item):
                            continue
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


def _download_to(tm, file_id: str, local_path: str, max_attempts: int = 10) -> None:
    """
    Download a file from OneDrive. Takes a TokenManager so long ingest runs
    survive the ~60-min access-token TTL.

    Retry policy:
      401 → force token refresh, retry immediately (does not consume an attempt)
      429 / 5xx → honor Retry-After header when present, else exponential backoff
      max_attempts (default 10) covers sustained Graph throttling windows; we
      raise the previous cap (was 2) because Microsoft sometimes asks for 5+ min
      and we were burning retries before the throttle window passed.
    """
    import time
    import requests as req_lib
    GRAPH = "https://graph.microsoft.com/v1.0"
    url   = f"{GRAPH}/me/drive/items/{file_id}/content"
    session = _get_http_session()
    refreshed_once = False
    for attempt in range(max_attempts):
        headers = {"Authorization": f"Bearer {tm.get()}"}
        try:
            # stream=True so we don't materialize the full response body in RAM
            # before writing — important for large PDFs/PPTX in a 1TB library.
            with session.get(url, headers=headers, timeout=300, stream=True) as resp:
                if resp.status_code == 401 and not refreshed_once:
                    log.warning("Graph 401 on download %s — refreshing token", file_id)
                    tm.force_refresh()
                    refreshed_once = True
                    continue
                if resp.status_code in (429, 500, 502, 503, 504):
                    # Honor Retry-After if Graph tells us how long to wait —
                    # otherwise exponential backoff capped at 5 min.
                    ra = resp.headers.get("Retry-After")
                    if ra and ra.isdigit():
                        wait = min(int(ra), 300)
                    else:
                        wait = min(2 ** attempt, 300)
                    log.warning(
                        "Graph %d on download %s — retry in %ds (attempt %d/%d)",
                        resp.status_code, file_id, wait, attempt + 1, max_attempts,
                    )
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                return
        except req_lib.RequestException as e:
            wait = min(2 ** attempt, 300)
            log.warning(
                "Download network error %s: %r — retry in %ds (attempt %d/%d)",
                file_id, e, wait, attempt + 1, max_attempts,
            )
            time.sleep(wait)
    raise RuntimeError(f"Download failed for {file_id} after {max_attempts} attempts")


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

def _cmd_set_priority(folders: list[str]) -> None:
    """
    Set the priority folder list. Writes /var/pkp/priority.json so the
    indexer (now or on next start) processes those folders first.
    """
    try:
        normalized = _normalize_priority_folders(folders)
    except ValueError as e:
        log.error("%s", e)
        sys.exit(2)

    if not normalized:
        log.error("No valid priority folders provided")
        sys.exit(2)

    # Detect dropped overlaps (parent already covers child).
    dropped = [f.strip().rstrip("/") for f in folders]
    dropped = [f for f in dropped if f and f.startswith("/") and f not in normalized]
    if dropped:
        log.info("Consolidated priority folders (dropped %s as redundant)", dropped)

    global _priority_cache
    with _priority_lock:
        _priority_cache = {
            "folders":       normalized,
            "started_at":    datetime.now(tz=timezone.utc).isoformat(),
            "files_done":    0,
            "restart_count": 0,
            "in_progress_file_ids": [],
            "in_progress_crashes":  {},
        }
        _save_priority()

    log.info("Priority set to %s", normalized)
    log.info("Restart pkp-full-indexer to begin priority indexing.")


def _cmd_clear_priority() -> None:
    """Remove the priority ledger entirely."""
    if not PRIORITY_FILE.exists():
        log.info("No active priority — nothing to clear.")
        return
    _clear_priority_file()
    log.info("Priority cleared. The indexer will resume normal full-library order.")


def _cmd_show_priority() -> None:
    """Print the current priority state."""
    ledger = _load_priority()
    if not ledger:
        print("No active priority indexing.")
        return
    print(json.dumps(ledger, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="PKP ingestion pipeline runner")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--local", metavar="FOLDER", help="Index files from a local folder (no OneDrive)")
    group.add_argument("--full",  action="store_true", help="Full OneDrive index (uses priority.json if present)")
    group.add_argument("--delta", action="store_true", help="Incremental OneDrive sync (delta)")
    group.add_argument(
        "--set-priority", nargs="+", metavar="FOLDER",
        help=f"Set priority folders for the next --full run (max {MAX_PRIORITY_FOLDERS}). "
             "Folders are walked first; rest of library follows.",
    )
    group.add_argument("--clear-priority", action="store_true", help="Remove the priority ledger.")
    group.add_argument("--show-priority",  action="store_true", help="Print the current priority state.")
    parser.add_argument("--force", action="store_true", help="Re-index even if content hash is unchanged")
    args = parser.parse_args()

    if args.local:
        run_local(args.local, force=args.force)
    elif args.full:
        run_full(force=args.force)
    elif args.delta:
        run_delta()
    elif args.set_priority:
        _cmd_set_priority(args.set_priority)
    elif args.clear_priority:
        _cmd_clear_priority()
    elif args.show_priority:
        _cmd_show_priority()


if __name__ == "__main__":
    main()
