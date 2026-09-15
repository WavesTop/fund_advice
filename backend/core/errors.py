from dataclasses import asdict, dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class ErrorBody:
    code: str
    message: str
    details: Any = None


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, details: Any = None):
        super().__init__(message)
        self.body = ErrorBody(code, message, details)
        self.status_code = status_code


async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": asdict(exc.body)})


async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "内部服务错误", "details": None}})
