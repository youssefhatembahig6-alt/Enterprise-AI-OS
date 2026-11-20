"""Single error format: {"error": {"code", "message", "request_id"}}."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_setup import request_id_var


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def error_response(
    status_code: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": request_id_var.get()}},
        headers=headers,
    )


def unauthenticated() -> ApiError:
    """Return a new generic 401 for every failure.

    Never raise a shared module-level instance: each raise attaches traceback
    frames to the exception object, so a shared instance would keep earlier
    requests' frames (and their locals) alive for the life of the process.
    """

    return ApiError(
        401, "unauthenticated", "Authentication required.", {"WWW-Authenticate": "Bearer"}
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations only; never echo submitted values (they may be passwords).
        fields = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()})
        return error_response(
            422, "invalid_request", "Invalid request fields: " + ", ".join(fields)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        return error_response(500, "internal_error", "Internal error.")
