"""One answer for a request the sidecar's own validation refuses.

Stores and routers signal "this request cannot be honoured as sent" with a
``ValueError`` whose text names the reason (``ingress_receipt_scope_mismatch``,
"source requires session_id"). Most routes translate the ones they expect;
one that escapes used to become a bare 500 with the reason only in the log.
The handler below answers 422 with that reason instead, for every route.

A ``ValueError`` subclass defined outside this package (``json.JSONDecodeError``,
``UnicodeDecodeError``, pydantic's ``ValidationError``) is a library refusing
the sidecar's own data, not the caller's request: it is re-raised and stays a
server error.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

INVALID_REQUEST = "invalid_request"


def is_request_refusal(exc: BaseException) -> bool:
    """True for a plain ``ValueError`` or one this package defines."""
    if not isinstance(exc, ValueError):
        return False
    cls = type(exc)
    return cls is ValueError or cls.__module__.split(".", 1)[0] == __name__.split(".", 1)[0]


async def refused_request(request: Request, exc: ValueError) -> JSONResponse:
    if not is_request_refusal(exc):
        raise exc
    logger.info("%s %s refused: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=422, content={"detail": {"code": INVALID_REQUEST, "message": str(exc) or INVALID_REQUEST}})


def install_exception_handlers(app: FastAPI) -> FastAPI:
    app.add_exception_handler(ValueError, refused_request)
    return app
