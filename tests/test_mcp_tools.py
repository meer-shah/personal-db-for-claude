"""
MCP endpoint integration tests — uses FastAPI TestClient + the live Qdrant instance.
Requires: Qdrant running on localhost:6333, QDRANT_HOST/PORT in .env.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

TOKEN = os.environ.get("MCP_BEARER_TOKEN", "test-token-for-local-runs-only")
os.environ["MCP_BEARER_TOKEN"] = TOKEN
os.environ.setdefault("QDRANT_HOST", "localhost")
os.environ.setdefault("QDRANT_PORT", "6333")

from main import app

client = TestClient(app, raise_server_exceptions=True)
HDR    = {"Authorization": f"Bearer {TOKEN}"}


# ── /health ───────────────────────────────────────────────────────────────────

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ── Auth guard ────────────────────────────────────────────────────────────────

def test_search_no_auth_returns_422_or_401():
    r = client.post("/tools/search_documents", json={"query": "test"})
    assert r.status_code in (401, 422)


def test_search_bad_token_returns_401():
    r = client.post(
        "/tools/search_documents",
        headers={"Authorization": "Bearer wrongtoken"},
        json={"query": "test"},
    )
    assert r.status_code == 401


def test_index_status_no_auth_returns_422_or_401():
    r = client.get("/tools/index_status")
    assert r.status_code in (401, 422)


# ── /tools/index_status ───────────────────────────────────────────────────────

def test_index_status_returns_valid_schema():
    r = client.get("/tools/index_status", headers=HDR)
    assert r.status_code == 200
    body = r.json()
    for field in ("indexed_files", "total_files", "percent_complete", "total_chunks",
                  "currently_indexing", "errors"):
        assert field in body, f"Missing field: {field}"
    assert isinstance(body["errors"], list)
    assert isinstance(body["total_chunks"], int)
    assert isinstance(body["percent_complete"], float)


# ── /tools/search_documents ───────────────────────────────────────────────────

def test_search_returns_valid_schema():
    r = client.post("/tools/search_documents", headers=HDR, json={"query": "project"})
    assert r.status_code == 200
    body = r.json()
    assert "results" in body
    assert "confident" in body
    assert "total_found" in body
    assert isinstance(body["results"], list)


def test_search_result_fields():
    r = client.post("/tools/search_documents", headers=HDR, json={"query": "deadline"})
    assert r.status_code == 200
    for result in r.json()["results"]:
        for field in ("text", "score", "confident", "file_name", "file_path",
                      "file_type", "chunk_type"):
            assert field in result, f"Missing field in result: {field}"


def test_search_top_k_respected():
    r = client.post("/tools/search_documents", headers=HDR, json={"query": "data", "top_k": 2})
    assert r.status_code == 200
    assert len(r.json()["results"]) <= 2


def test_search_file_type_filter():
    r = client.post(
        "/tools/search_documents", headers=HDR,
        json={"query": "contract", "file_type_filter": "xlsx"},
    )
    assert r.status_code == 200
    for result in r.json()["results"]:
        assert result["file_type"] == "xlsx"


def test_search_date_filter_invalid_date_returns_error_or_empty():
    # Should NOT crash (was previously returning 500)
    r = client.post(
        "/tools/search_documents", headers=HDR,
        json={"query": "test", "date_from": "not-a-date"},
    )
    assert r.status_code in (200, 422)


def test_search_date_filter_valid_iso_date():
    r = client.post(
        "/tools/search_documents", headers=HDR,
        json={"query": "test", "date_from": "2020-01-01", "date_to": "2030-12-31"},
    )
    assert r.status_code == 200


def test_search_folder_filter_prefix():
    # Folder prefix filter — should not crash; results may be 0 if no match
    r = client.post(
        "/tools/search_documents", headers=HDR,
        json={"query": "contract", "folder_filter": "/tmp"},
    )
    assert r.status_code == 200


def test_search_author_filter():
    r = client.post(
        "/tools/search_documents", headers=HDR,
        json={"query": "budget", "author_filter": "Alice"},
    )
    assert r.status_code == 200


def test_search_low_confidence_query():
    r = client.post(
        "/tools/search_documents", headers=HDR,
        json={"query": "xkzqjfwpvmbn gibberish zzzzz"},
    )
    assert r.status_code == 200
    body = r.json()
    # Gibberish query — may return results but confident should be False
    if body["results"]:
        assert body["confident"] is False or body["results"][0]["score"] < 0.70


# ── /tools/get_document ───────────────────────────────────────────────────────

def test_get_document_not_found():
    r = client.post(
        "/tools/get_document", headers=HDR,
        json={"file_path": "/nonexistent/file.docx"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is False
    assert body["chunks"] == []


def test_get_document_returns_chunks_sorted():
    # Find a real file_path from search first
    sr = client.post("/tools/search_documents", headers=HDR, json={"query": "data", "top_k": 1})
    results = sr.json()["results"]
    if not results:
        pytest.skip("No documents indexed — skipping get_document test")

    file_path = results[0]["file_path"]
    r = client.post("/tools/get_document", headers=HDR, json={"file_path": file_path})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert len(body["chunks"]) >= 1
    indices = [c["chunk_index"] for c in body["chunks"]]
    assert indices == sorted(indices)


# ── /tools/save_to_onedrive ───────────────────────────────────────────────────

def test_save_blocked_folder_returns_400():
    r = client.post(
        "/tools/save_to_onedrive", headers=HDR,
        json={"filename": "test.txt", "content": "hello", "folder_path": "/.trash"},
    )
    assert r.status_code == 400


def test_save_returns_error_gracefully_when_no_token():
    # Without a valid OneDrive refresh token this will fail — but should not crash
    r = client.post(
        "/tools/save_to_onedrive", headers=HDR,
        json={"filename": "test.txt", "content": "hello", "folder_path": "/Outputs"},
    )
    assert r.status_code == 200
    body = r.json()
    # Either success (if Azure is enabled) or a graceful error dict
    assert "success" in body
    assert "error" in body
