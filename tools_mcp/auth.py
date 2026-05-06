import os
from pathlib import Path
from fastapi import Header, HTTPException, status
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def require_bearer(authorization: str = Header(...)) -> None:
    """FastAPI dependency — validates Bearer token on every request."""
    expected = os.getenv("MCP_BEARER_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="MCP_BEARER_TOKEN not configured on server",
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
