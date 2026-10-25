import asyncio
import contextvars
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import psutil
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, importer
from .routes_bench import router as bench_router
from .routes_imports import UPLOADS, router as imports_router
from .routes_tx import router as tx_router

request_id = contextvars.ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id.get()
        return True


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
for handler in logging.getLogger().handlers:
    handler.addFilter(RequestIdFilter())
log = logging.getLogger("mondje")


async def purge_loop():
    while True:
        await asyncio.sleep(60)
        try:
            async with db.pool.acquire() as conn:
                await conn.execute("DELETE FROM transactions WHERE deleted_at < now() - interval '24 hours'")
        except Exception:
            log.exception("purge des suppressions échouée")


@asynccontextmanager
async def lifespan(app):
    await db.init()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    for leftover in UPLOADS.glob("*.part"):
        leftover.unlink(missing_ok=True)
    await importer.resume_all()
    purge = asyncio.create_task(purge_loop())
    yield
    purge.cancel()
    pending = list(importer.tasks.values())
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await db.close()


class RequestIdMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")[:64]
        rid = incoming or uuid.uuid4().hex[:12]
        token = request_id.set(rid)
        scope.setdefault("state", {})["request_id"] = rid
        started = time.perf_counter()
        status = 0

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            log.info("%s %s -> %s en %.1f ms", scope["method"], scope["path"], status,
                     (time.perf_counter() - started) * 1000)
            request_id.reset(token)


app = FastAPI(title="Mondje", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(RequestIdMiddleware)


def rid_of(request):
    return getattr(request.state, "request_id", "-")


@app.exception_handler(RequestValidationError)
async def on_validation(request: Request, exc: RequestValidationError):
    details = [
        {"champ": ".".join(str(p) for p in e["loc"] if p != "body"), "raison": e["msg"]} for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"erreur": "donnée invalide", "details": details, "request_id": rid_of(request)},
        headers={"x-request-id": rid_of(request)},
    )


@app.exception_handler(StarletteHTTPException)
async def on_http(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"erreur": str(exc.detail), "request_id": rid_of(request)},
        headers={"x-request-id": rid_of(request)},
    )


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    request_id.set(rid_of(request))
    log.exception("erreur interne")
    return JSONResponse(
        status_code=500,
        content={"erreur": "erreur interne du serveur", "request_id": rid_of(request)},
        headers={"x-request-id": rid_of(request)},
    )


@app.get("/api/health")
async def health():
    rss = psutil.Process().memory_info().rss
    return {"status": "ok", "rss_mb": round(rss / 1048576, 1), "imports_actifs": len(importer.tasks)}


app.include_router(tx_router, prefix="/api")
app.include_router(imports_router, prefix="/api")
app.include_router(bench_router, prefix="/api")
app.mount("/", StaticFiles(directory=os.getenv("FRONTEND_DIR", "frontend"), html=True), name="front")
