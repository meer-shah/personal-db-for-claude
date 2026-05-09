"""
PKP MCP Server — FastAPI entry point.
Start with: uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
"""

import sys
import os
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))

from contextlib import asynccontextmanager
from fastapi import FastAPI
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(REPO_ROOT / ".env")

from tools_mcp.search            import router as search_router
from tools_mcp.get_document      import router as get_document_router
from tools_mcp.index_status      import router as index_status_router
from tools_mcp.list_files        import router as list_files_router
from tools_mcp.create_word       import router as create_word_router
from tools_mcp.create_excel      import router as create_excel_router
from tools_mcp.create_powerpoint import router as create_ppt_router
from tools_mcp.upload            import router as upload_router
from mcp_sse                     import mcp_lifespan, mcp_asgi, get_sse_starlette_app

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp_lifespan(app):
        yield


app = FastAPI(
    title="PKP MCP Server",
    description="Personal Knowledge Platform — MCP tools for Claude.ai",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(search_router)
app.include_router(get_document_router)
app.include_router(index_status_router)
app.include_router(list_files_router)
app.include_router(create_word_router)
app.include_router(create_excel_router)
app.include_router(create_ppt_router)
app.include_router(upload_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def _no_oauth_resource():
    # Signal "no OAuth required" — return 404 with a JSON body so Claude.ai falls
    # back to bearer-token / no-auth mode. Returning a real document with no
    # auth_servers also works as a hint that the resource is publicly accessible.
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=404, content={"error": "not_found"})


# MCP transports (registered after regular routes):
# - /mcp  → Streamable HTTP (used by Claude.ai)
# - /sse  → legacy SSE (kept as fallback)
#
# Some clients (Claude.ai) request /mcp without trailing slash, while a Mount
# only matches /mcp/. We register a raw Starlette route that catches /mcp
# and forwards the ASGI scope to the streamable-http handler with path "/".
from starlette.routing import Route as _StarletteRoute


async def _mcp_endpoint(request):
    scope = {**request.scope, "path": "/", "raw_path": b"/"}
    # Re-create receive callable from request body
    body = await request.body()
    body_sent = False

    async def _receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    sent: dict = {"status": 200, "headers": [], "body": b""}

    async def _send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    await mcp_asgi(scope, _receive, _send)
    from starlette.responses import Response
    return Response(
        content=sent["body"],
        status_code=sent["status"],
        headers={k.decode(): v.decode() for k, v in sent["headers"]},
    )


app.router.routes.insert(
    0,
    _StarletteRoute("/mcp", _mcp_endpoint, methods=["GET", "POST", "DELETE"]),
)
app.mount("/mcp", mcp_asgi)
app.mount("/", get_sse_starlette_app())
