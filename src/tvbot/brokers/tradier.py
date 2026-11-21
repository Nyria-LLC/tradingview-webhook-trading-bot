"""Tradier brokerage API adapter (plain HTTP, no SDK)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from .base import (
    Broker,
    BrokerRejected,
    BrokerResult,
    GuardRefused,
    OrderIntent,
    OutcomeUnknown,
    dec_str,
    is_whole,
    json_or_none,
)

LIVE_BASE = "https://api.tradier.com/v1"
SANDBOX_BASE = "https://sandbox.tradier.com/v1"
ACCOUNT_PLACEHOLDER = "<TRADIER_ACCOUNT_ID>"


def errors_text(errors: Any) -> str:
    """The strings under a Tradier `errors` property joined with "; "."""
    if isinstance(errors, dict):
        inner = errors.get("error")
        if isinstance(inner, list):
            return "; ".join(str(item) for item in inner)
        if isinstance(inner, str):
            return inner
    return str(errors)


class TradierBroker(Broker):
    name = "tradier"
    required_env = ("TRADIER_ACCESS_TOKEN", "TRADIER_ACCOUNT_ID")
    reads_positions = True

    def _base(self) -> str:
        return LIVE_BASE if self.settings.tradier_live else SANDBOX_BASE

    def _account(self, placeholders: bool) -> str:
        if placeholders:
            return ACCOUNT_PLACEHOLDER
        return quote(self.settings.tradier_account_id, safe="")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.tradier_access_token}",
            "Accept": "application/json",
        }

    def validate(self, intent: OrderIntent) -> None:
        if not is_whole(intent.quantity):
            raise GuardRefused("Tradier orders must be whole shares")

    def build_request(self, intent: OrderIntent, cid: str | None, placeholders: bool) -> dict:
        form = {
            "class": "equity",
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": str(int(intent.quantity)),
            "type": intent.order_type,
            "duration": intent.time_in_force,
            "tag": cid,
        }
        if intent.order_type == "limit":
            form["price"] = dec_str(intent.limit_price)
        url = f"{self._base()}/accounts/{self._account(placeholders)}/orders"
        return {"method": "POST", "url": url, "form": form}

    def error_message(self, resp: Any) -> str:
        body = json_or_none(resp)
        if isinstance(body, dict) and "errors" in body:
            return errors_text(body["errors"])
        return resp.text

    def held_quantity(self, symbol: str) -> Decimal:
        url = f"{self._base()}/accounts/{self._account(False)}/positions"
        resp = self._send("GET", url, is_order=False, headers=self._headers())
        if not 200 <= resp.status_code < 300:
            raise BrokerRejected(resp.status_code, self.error_message(resp))
        body = json_or_none(resp)
        positions = body.get("positions") if isinstance(body, dict) else None
        if not isinstance(positions, dict):
            return Decimal(0)  # the docs show no empty example; anything else means none
        entry = positions.get("position")
        if isinstance(entry, list):
            items = entry
        elif isinstance(entry, dict):
            items = [entry]  # a single position comes back as one object
        else:
            items = []
        for item in items:
            if not isinstance(item, dict) or item.get("symbol") != symbol:
                continue
            try:
                held = Decimal(str(item["quantity"]))
            except (InvalidOperation, KeyError):
                raise BrokerRejected(resp.status_code, "unexpected position quantity from Tradier") from None
            if not held.is_finite():
                raise BrokerRejected(resp.status_code, "unexpected position quantity from Tradier")
            return held if held > 0 else Decimal(0)  # negative means short
        return Decimal(0)

    def prepare(self, intent: OrderIntent, cid: str | None) -> dict:
        return self.build_request(intent, cid, placeholders=False)

    def send(self, prepared: dict, intent: OrderIntent, cid: str | None) -> BrokerResult:
        resp = self._send(
            "POST", prepared["url"], is_order=True, cid=cid, headers=self._headers(), data=prepared["form"]
        )
        status = resp.status_code
        if status >= 500:
            raise OutcomeUnknown(cid, f"tradier returned HTTP {status}")
        if not 200 <= status < 300:
            raise BrokerRejected(status, self.error_message(resp))
        # Downstream refusals come back as HTTP 200 with an `errors` property.
        body = json_or_none(resp)
        if isinstance(body, dict) and "errors" in body:
            raise BrokerRejected(status, errors_text(body["errors"]))
        order = body.get("order") if isinstance(body, dict) else None
        if not isinstance(order, dict):
            raise BrokerRejected(status, "unexpected response from Tradier")
        order_id = None
        if order.get("status") == "ok" and order.get("id") is not None:
            order_id = str(order["id"])
        return BrokerResult(order_id, status, cid, dec_str(intent.quantity))
