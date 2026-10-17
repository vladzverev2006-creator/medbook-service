"""Mock external service: Payment Gateway and Notification Gateway."""

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mock_gateway")

app = FastAPI(title="MedBook Mock Gateway", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory stores
NOTIFICATIONS: list[dict[str, Any]] = []
_PAY_MODE_FILE = "/tmp/medbook_pay_mode"
_current_pay_mode = "ok"


class PayRequest(BaseModel):
    appointment_id: int
    user_id: int
    amount: str | float
    currency: str = "KZT"
    idempotency_key: str | None = None
    mode: str | None = None


class NotifyRequest(BaseModel):
    user_id: int
    event: str
    message: str
    appointment_id: int | None = None
    email: str | None = None
    phone: str | None = None


class PayModeRequest(BaseModel):
    mode: str  # "ok" | "decline" | "error"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "mock-gateway"}


@app.post("/mock/pay")
async def pay(
    payload: PayRequest,
    x_mock_pay_mode: str | None = Header(None, alias="X-Mock-Pay-Mode"),
    mode_query: str | None = Query(None, alias="mode"),
) -> JSONResponse:
    """Mock payment gateway. Can simulate failures via header, query param, body or global mode."""
    mode = x_mock_pay_mode or mode_query or payload.mode
    if not mode:
        if os.path.exists(_PAY_MODE_FILE):
            try:
                with open(_PAY_MODE_FILE) as f:
                    mode = f.read().strip()
            except OSError:
                mode = _current_pay_mode
        else:
            mode = _current_pay_mode

    mode = (mode or "ok").lower()
    logger.info(
        "Payment request: appt=%s, user=%s, amount=%s, mode=%s",
        payload.appointment_id,
        payload.user_id,
        payload.amount,
        mode,
    )

    if mode == "decline":
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content={"status": "declined", "detail": "insufficient funds"},
        )
    if mode == "error":
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "detail": "Payment gateway internal error"},
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "success",
            "transaction_id": f"tx-{payload.appointment_id}",
            "amount": str(payload.amount),
            "currency": payload.currency,
        },
    )


@app.post("/mock/pay/mode")
def set_pay_mode(payload: PayModeRequest) -> dict[str, str]:
    global _current_pay_mode
    _current_pay_mode = payload.mode.lower()
    try:
        with open(_PAY_MODE_FILE, "w") as f:
            f.write(_current_pay_mode)
    except OSError:
        pass
    return {"status": "ok", "pay_mode": _current_pay_mode}


@app.get("/mock/pay/mode")
def get_pay_mode() -> dict[str, str]:
    return {"pay_mode": _current_pay_mode}


@app.post("/mock/notify")
async def notify(payload: NotifyRequest) -> dict[str, Any]:
    """Mock notification gateway (SMS/Email)."""
    record = payload.model_dump()
    NOTIFICATIONS.append(record)
    logger.info("Notification received: event=%s, user=%s", payload.event, payload.user_id)
    return {"status": "sent", "event": payload.event, "appointment_id": payload.appointment_id}


@app.get("/mock/notifications")
def list_notifications() -> list[dict[str, Any]]:
    return NOTIFICATIONS


@app.delete("/mock/notifications")
def clear_notifications() -> dict[str, str]:
    NOTIFICATIONS.clear()
    return {"status": "cleared"}
