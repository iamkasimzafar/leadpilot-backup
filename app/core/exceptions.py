"""Application error types and the handlers that render them as JSON.

Services raise these; the API layer never needs to build error responses by hand.
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base class for expected, user-facing application errors."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Any = None,
        challenge: str | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details
        self.challenge = challenge
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource was not found."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The resource already exists."


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"
    message = "Not authenticated."


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"
    message = "You do not have permission to perform this action."


class EmailNotVerifiedError(AppError):
    """Sign-in refused because the address has not been confirmed yet."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "email_not_verified"
    message = "Please verify your email address before signing in."


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"
    message = "The submitted data is invalid."


def _error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return body


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers so every error shares one response shape."""

    @app.exception_handler(AppError)
    async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": exc.challenge} if exc.challenge else None
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message, exc.details),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_body(
                "validation_error",
                "The submitted data is invalid.",
                # jsonable so non-serialisable ctx values can't break the response.
                [
                    {
                        "loc": list(err.get("loc", [])),
                        "msg": err.get("msg", ""),
                        "type": err.get("type", ""),
                    }
                    for err in exc.errors()
                ],
            ),
        )
