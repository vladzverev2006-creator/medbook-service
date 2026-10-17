"""HTTP client for mock-gateway: payments (`/mock/pay`) and notifications (`/mock/notify`)."""

import logging
from decimal import Decimal
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_client = httpx.Client(
    base_url=settings.MOCK_SERVICE_URL,
    timeout=httpx.Timeout(settings.EXTERNAL_TIMEOUT),
)


class PaymentError(Exception):
    """Base payment failure."""


class PaymentGatewayUnavailable(PaymentError):
    """Network error, timeout or 5xx from the payment gateway."""


class PaymentDeclined(PaymentError):
    """Gateway answered, but refused the payment."""


def pay(appointment_id: int, user_id: int, amount: Decimal) -> dict[str, Any]:
    payload = {
        "appointment_id": appointment_id,
        "user_id": user_id,
        "amount": str(amount),
        "currency": "KZT",
        "idempotency_key": f"appointment-{appointment_id}",
    }
    try:
        response = _client.post("/mock/pay", json=payload)
    except httpx.HTTPError as exc:
        logger.error("Payment gateway unreachable for appointment %s: %r", appointment_id, exc)
        raise PaymentGatewayUnavailable(str(exc)) from exc

    if response.status_code >= 500:
        logger.error("Payment gateway error %s for appointment %s", response.status_code, appointment_id)
        raise PaymentGatewayUnavailable(f"HTTP {response.status_code}")
    if response.status_code >= 400:
        raise PaymentDeclined(f"HTTP {response.status_code}: {response.text[:200]}")

    try:
        data = response.json()
    except ValueError:
        data = {}
    if isinstance(data, dict) and str(data.get("status", "")).lower() in {"failed", "declined", "error"}:
        raise PaymentDeclined(str(data.get("detail") or data.get("status")))
    return data if isinstance(data, dict) else {}


def notify(
    user_id: int,
    event: str,
    message: str,
    appointment_id: int | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> bool:
    """Send SMS/Email notification. Never raises on network errors: a broken
    SMS gateway must not break booking. Returns True on success."""
    payload = {
        "user_id": user_id,
        "event": event,
        "message": message,
        "appointment_id": appointment_id,
        "email": email,
        "phone": phone,
    }
    try:
        response = _client.post("/mock/notify", json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "Notification '%s' for appointment %s failed: %r", event, appointment_id, exc
        )
        return False
    return True


def close_client() -> None:
    _client.close()
