"""Validation of the JSON alert TradingView posts to /webhook."""

from __future__ import annotations

import hmac
import math
import re
from decimal import Decimal
from typing import Any

from .brokers.base import OrderIntent
from .config import BROKER_NAMES, Settings

_SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,9}")
_NUMERIC_TEXT = re.compile(r"\s*[0-9]+(\.[0-9]+)?\s*")


class AlertError(Exception):
    """A refusal with the HTTP status and one-line message sent back to the caller."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def check_secret(payload: dict, settings: Settings) -> None:
    """Raise AlertError(401) unless payload["secret"] equals WEBHOOK_SECRET."""
    supplied = payload.get("secret")
    if not isinstance(supplied, str):
        raise AlertError(401, "unauthorized")
    try:
        supplied_bytes = supplied.encode("utf-8")
        expected_bytes = settings.webhook_secret.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate from a JSON \ud800 escape cannot be UTF-8 encoded; it cannot match.
        raise AlertError(401, "unauthorized") from None
    if not hmac.compare_digest(supplied_bytes, expected_bytes):
        raise AlertError(401, "unauthorized")


def _number(value: Any) -> Decimal | None:
    """Return a finite Decimal for an int, float or plain decimal string; otherwise None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Decimal(str(value))
    if isinstance(value, str):
        if not _NUMERIC_TEXT.fullmatch(value):
            return None
        return Decimal(value.strip())
    return None


def _choice(payload: dict, key: str, allowed: tuple[str, ...], default: str, message: str) -> str:
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        raise AlertError(422, message)
    return value.strip().lower()


def parse_alert(payload: dict, settings: Settings) -> OrderIntent:
    """Turn a checked payload into an OrderIntent; each failure is an AlertError(422)."""
    broker = _choice(payload, "broker", BROKER_NAMES, settings.default_broker, "unknown broker")

    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not symbol.isascii():
        raise AlertError(422, "invalid symbol")
    symbol = symbol.strip()
    if ":" in symbol:
        symbol = symbol.rsplit(":", 1)[1]
    symbol = symbol.upper()
    if not _SYMBOL.fullmatch(symbol):
        raise AlertError(422, "invalid symbol")
    if settings.allowed_symbols and symbol not in settings.allowed_symbols:
        raise AlertError(422, "symbol not allowed")

    action = payload.get("action")
    if not isinstance(action, str) or action.strip().lower() not in ("buy", "sell"):
        raise AlertError(422, "action must be buy or sell")
    side = action.strip().lower()

    quantity = _number(payload.get("quantity"))
    if quantity is None or quantity <= 0:
        raise AlertError(422, "quantity must be a positive number")
    if quantity > settings.max_qty:
        raise AlertError(422, "quantity exceeds MAX_QTY")

    order_type = _choice(payload, "order_type", ("market", "limit"), "market", "order_type must be market or limit")

    limit_price = None
    if order_type == "limit":
        limit_price = _number(payload.get("limit_price"))
        if limit_price is None or limit_price <= 0:
            raise AlertError(422, "limit_price is required for limit orders")

    time_in_force = _choice(payload, "time_in_force", ("day", "gtc"), "day", "time_in_force must be day or gtc")

    return OrderIntent(
        broker=broker,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        time_in_force=time_in_force,
    )
