"""
get_document tool — retrieve all chunks for a given file_path, sorted by chunk_index.
"""

import os
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

from tools_mcp.auth import require_bearer

router = APIRouter()
COLLECTION = "pkp_chunks"


class GetDocumentRequest(BaseModel):
    file_path: str


class ChunkDetail(BaseModel):
    chunk_index:  int
    chunk_type:   str
    text:         str
    page_number:  int | None
    slide_number: int | None
    sheet_name:   str | None


class GetDocumentResponse(BaseModel):
    file_path:  str
    file_name:  str
    file_type:  str
    author:     str | None
    chunks:     list[ChunkDetail]
    found:      bool


def _get_qdrant() -> QdrantClient:
    host = os.getenv("QDRANT_HOST", "localhost")
    port = int(os.getenv("QDRANT_PORT", 6333))
    return QdrantClient(host=host, port=port)


@router.post("/tools/get_document", response_model=GetDocumentResponse)
def get_document(
    req: GetDocumentRequest,
    _: None = Depends(require_bearer),
) -> GetDocumentResponse:
    client = _get_qdrant()

    # Scroll through all points matching this file_path
    results, _ = client.scroll(
        collection_name=COLLECTION,
        scroll_filter=Filter(
            must=[FieldCondition(key="file_path", match=MatchValue(value=req.file_path))]
        ),
        limit=10000,
        with_payload=True,
        with_vectors=False,
    )

    if not results:
        return GetDocumentResponse(
            file_path=req.file_path,
            file_name="",
            file_type="",
            author=None,
            chunks=[],
            found=False,
        )

    # Sort by chunk_index
    results.sort(key=lambda p: p.payload.get("chunk_index", 0))

    first = results[0].payload or {}
    chunks = [
        ChunkDetail(
            chunk_index  = p.payload.get("chunk_index", 0),
            chunk_type   = p.payload.get("chunk_type", "text"),
            text         = p.payload.get("text", ""),
            page_number  = p.payload.get("page_number"),
            slide_number = p.payload.get("slide_number"),
            sheet_name   = p.payload.get("sheet_name"),
        )
        for p in results
    ]

    return GetDocumentResponse(
        file_path = req.file_path,
        file_name = first.get("file_name", ""),
        file_type = first.get("file_type", ""),
        author    = first.get("author"),
        chunks    = chunks,
        found     = True,
    )
