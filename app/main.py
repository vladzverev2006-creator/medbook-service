import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import api_router
from app.api.appointments import cleanup_expired_pending_appointments
from app.config import settings
from app.database import SessionLocal, engine, get_db
from app.redis_client import invalidate_cache, redis_client
from app.services import external

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("medbook.http")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
INDEX_FILE = os.path.join(STATIC_DIR, "index.html")


async def _expired_pending_cleanup_loop() -> None:
    """Background worker: cancels PENDING appointments whose Redis lock expired."""
    while True:
        await asyncio.sleep(5)
        try:
            with SessionLocal() as db:
                cleanup_expired_pending_appointments(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Background pending cleanup error: %r", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    cleanup_task = asyncio.create_task(_expired_pending_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        external.close_client()
        engine.dispose()


app = FastAPI(
    title="MedBook API",
    description="Онлайн-запись к врачам",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Process-Time-Ms"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.exception(
            "%s %s -> 500 (%.1f ms) unhandled error", request.method, request.url.path, elapsed_ms
        )
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    level = logging.ERROR if response.status_code >= 500 else logging.INFO
    logger.log(
        level, "%s %s -> %d (%.1f ms)",
        request.method, request.url.path, response.status_code, elapsed_ms,
    )
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.1f}"
    return response


@app.exception_handler(RedisError)
async def redis_error_handler(request: Request, exc: RedisError) -> JSONResponse:
    # Only lock operations propagate RedisError (cache helpers swallow it):
    # booking without a lock would break consistency, so we refuse (CP).
    logging.getLogger("medbook").error("Redis unavailable during %s: %r", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "Сервис бронирования временно недоступен, попробуйте позже"},
    )


app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
def health() -> JSONResponse:
    components: dict[str, str] = {}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        components["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Health check: database unavailable: %r", exc)
        components["database"] = "unavailable"
    try:
        redis_client.ping()
        components["redis"] = "ok"
    except RedisError as exc:
        logger.warning("Health check: redis unavailable: %r", exc)
        components["redis"] = "unavailable"

    db_ok = components["database"] == "ok"
    overall = "ok" if all(v == "ok" for v in components.values()) else "degraded"
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={"status": overall if db_ok else "unavailable", "components": components},
    )


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def serve_index():
    """Serves the Material Design 3 Single Page Application."""
    if os.path.exists(INDEX_FILE):
        return FileResponse(INDEX_FILE, media_type="text/html")
    return JSONResponse({"status": "MedBook API is running", "docs": "/docs"})


# Static files mount if directory exists
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Mock gateway proxy routes & demo simulation helpers
@app.get("/api/v1/mock/notifications", tags=["mock-proxy"])
def proxy_mock_notifications():
    try:
        r = external._client.get("/mock/notifications")
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/v1/mock/pay/mode", tags=["mock-proxy"])
async def proxy_mock_pay_mode(request: Request):
    try:
        body = await request.json()
        r = external._client.post("/mock/pay/mode", json=body)
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/v1/mock/pay/mode", tags=["mock-proxy"])
def proxy_get_mock_pay_mode():
    try:
        r = external._client.get("/mock/pay/mode")
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/v1/mock/expire-locks", tags=["mock-proxy"])
def expire_all_slot_locks(db: Session = Depends(get_db)) -> dict[str, int | str]:
    """Demo helper: delete all `lock:slot:*` keys in Redis and cancel expired PENDING bookings."""
    invalidate_cache("lock:slot:*")
    cancelled = cleanup_expired_pending_appointments(db)
    return {
        "status": "ok",
        "cancelled": cancelled,
        "cancelled_appointments": cancelled,
    }
