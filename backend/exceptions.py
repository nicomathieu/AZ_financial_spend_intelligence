from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class APIError(Exception):
    """Base class for all application errors. Subclasses set status_code."""
    status_code: int = 500

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class ValidationError(APIError):
    status_code = 422


class NotFoundError(APIError):
    status_code = 404


class LLMUnavailableError(APIError):
    status_code = 503


# ── Exception handlers ────────────────────────────────────────────────────────
# Registered in create_app() so all HTTP translation is in one place.

async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": type(exc).__name__, "detail": exc.detail, "path": str(request.url.path)},
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "InternalServerError", "detail": "An unexpected error occurred.", "path": str(request.url.path)},
    )
