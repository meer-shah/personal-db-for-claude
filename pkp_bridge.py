"""
PKP stdio bridge for Claude Desktop (Windows / macOS / Linux).

Claude Desktop spawns this script as a local subprocess and talks to it over stdio.
This script forwards each MCP tool call to the remote PKP server over HTTPS using
the bearer token.

Setup on your laptop:
    1. Install Python 3.10+
    2. pip install mcp httpx
    3. Add this entry to claude_desktop_config.json (see README at bottom of file)

Environment variables (set in claude_desktop_config.json, NOT here):
    PKP_SERVER_URL    e.g. https://46-225-18-94.nip.io
    PKP_BEARER_TOKEN  the same token as MCP_BEARER_TOKEN on the server

Restart Claude Desktop after editing the config.
"""

import asyncio
import json
import os
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


SERVER_URL = os.environ.get("PKP_SERVER_URL", "").rstrip("/")
BEARER     = os.environ.get("PKP_BEARER_TOKEN", "")

if not SERVER_URL or not BEARER:
    print("ERROR: set PKP_SERVER_URL and PKP_BEARER_TOKEN env vars in claude_desktop_config.json",
          file=sys.stderr)
    sys.exit(1)

HEADERS = {"Authorization": f"Bearer {BEARER}"}
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

server = Server("pkp-knowledge-base")


# ── Tool list ─────────────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_documents",
            description=(
                "Search the user's personal knowledge base for documents relevant to a query. "
                "Returns ranked text chunks with source file metadata. "
                "Use this to answer questions about the user's files and documents."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query":            {"type": "string",  "description": "The search query"},
                    "top_k":            {"type": "integer", "description": "Number of results (1-15)", "default": 5},
                    "file_type_filter": {"type": "string",  "description": "Filter by file type e.g. pdf, docx, xlsx, pptx"},
                    "folder_filter":    {"type": "string",  "description": "Filter by folder path prefix e.g. /Projects"},
                    "author_filter":    {"type": "string",  "description": "Filter by author (substring match)"},
                    "date_from":        {"type": "string",  "description": "Only docs modified after this ISO date e.g. 2024-01-01"},
                    "date_to":          {"type": "string",  "description": "Only docs modified before this ISO date e.g. 2024-12-31"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_document",
            description="Retrieve the full text content of a document by its file path. Use after search_documents.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Full file path from search_documents"},
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="index_status",
            description=(
                "Check the indexing status of the knowledge base — files indexed, "
                "percent complete, total chunks, and a small sample of errors. "
                "By default returns a compact summary that always fits any response "
                "cap. Pass include_errors=true to get a paginated full error list."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_errors": {"type": "boolean", "description": "Return a paginated slice of the full error list. Default false (returns a 5-error sample only)."},
                    "errors_limit":   {"type": "integer", "description": "Errors per page when include_errors=true (1-200, default 50)."},
                    "errors_offset":  {"type": "integer", "description": "Pagination offset when include_errors=true. Default 0."},
                },
            },
        ),
        Tool(
            name="save_to_onedrive",
            description="Save a text file to a OneDrive folder.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filename":    {"type": "string", "description": "Filename e.g. summary.txt"},
                    "content":     {"type": "string", "description": "Text content to write"},
                    "folder_path": {"type": "string", "description": "OneDrive folder e.g. /Outputs"},
                },
                "required": ["filename", "content", "folder_path"],
            },
        ),
    ]


# ── Tool dispatch ─────────────────────────────────────────────────────────────

ENDPOINTS = {
    "search_documents": ("POST", "/tools/search_documents"),
    "get_document":     ("POST", "/tools/get_document"),
    "index_status":     ("GET",  "/tools/index_status"),
    "save_to_onedrive": ("POST", "/tools/save_to_onedrive"),
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name not in ENDPOINTS:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    method, path = ENDPOINTS[name]
    url = SERVER_URL + path

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            if method == "GET":
                # Forward arguments as query params so tools like index_status
                # can accept include_errors=true / errors_limit / errors_offset.
                r = await client.get(url, headers=HEADERS, params=arguments or None)
            else:
                r = await client.post(url, headers=HEADERS, json=arguments)
        r.raise_for_status()
        body = r.json()
        return [TextContent(type="text", text=json.dumps(body, ensure_ascii=False, indent=2))]
    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"HTTP {e.response.status_code}: {e.response.text}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error calling {name}: {e}")]


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
