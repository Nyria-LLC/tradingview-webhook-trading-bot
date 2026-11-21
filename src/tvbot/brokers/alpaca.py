"""Alpaca Trading API adapter (plain HTTP, no SDK)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .base import (
    Broker,
    BrokerRejected,
    BrokerResult,
    GuardRefused,
    OrderIntent,
    dec_str,
    is_whole,
    json_or_none,
)

LIVE_BASE = "https://api.alpaca.markets"
PAPER_BASE = "https://paper-api.alpaca.markets"


def _decimal(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise BrokerRejected(None, "unexpected position quantity from Alpaca") from None
    if not number.is_finite():
        raise BrokerRejected(None, "unexpected position quantity from Alpaca")
    return number


class AlpacaBroker(Broker):
    name = "alpaca"
    required_env = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
    reads_positions = True

    def _base(self) -> str:
        return LIVE_BASE if self.settings.alpaca_live else PAPER_BASE

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.settings.alpaca_api_key_id,
            "APCA-API-SECRET-KEY": self.settings.alpaca_api_secret_key,
        }

    def validate(self, intent: OrderIntent) -> None:
        fractional_ok = intent.order_type == "market" and intent.time_in_force == "day"
        if not is_whole(intent.quantity) and not fractional_ok:
            raise GuardRefused("Alpaca accepts fractional quantities only for market day orders")

    def build_request(self, intent: OrderIntent, cid: str | None, placeholders: bool) -> dict:
        body = {
            "symbol": intent.symbol,
            "qty": dec_str(intent.quantity),
            "side": intent.side,
            "type": intent.order_type,
            "time_in_force": intent.time_in_force,
            "client_order_id": cid,
        }
        if intent.order_type == "limit":
            body["limit_price"] = dec_str(intent.limit_price)
        return {"method": "POST", "url": f"{self._base()}/v2/orders", "json": body}

    def error_message(self, resp: Any) -> str:
        body = json_or_none(resp)
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            return body["message"]
        return resp.text

    def held_quantity(self, symbol: str) -> Decimal:
        # The list endpoint answers 200 [] with no positions; the single-symbol endpoint's
        # no-position answer is not documented.
        resp = self._send("GET", f"{self._base()}/v2/positions", is_order=False, headers=self._headers())
        if not 200 <= resp.status_code < 300:
            raise BrokerRejected(resp.status_code, self.error_message(resp))
        positions = json_or_none(resp)
        if not isinstance(positions, list):
            raise BrokerRejected(resp.status_code, "unexpected positions response from Alpaca")
        for item in positions:
            if not isinstance(item, dict) or item.get("symbol") != symbol:
                continue
            if item.get("side") != "long":
                return Decimal(0)
            qty = _decimal(item.get("qty"))
            available = qty if item.get("qty_available") is None else _decimal(item["qty_available"])
            if qty > 0 and available == 0:
                raise GuardRefused(
                    f"the shares held in {symbol} are committed to open orders; none are available to sell"
                )
            return available
        return Decimal(0)

    def prepare(self, intent: OrderIntent, cid: str | None) -> dict:
        return self.build_request(intent, cid, placeholders=False)

    def send(self, prepared: dict, intent: OrderIntent, cid: str | None) -> BrokerResult:
        resp = self._send(
            "POST", prepared["url"], is_order=True, cid=cid, headers=self._headers(), json=prepared["json"]
        )
        self.order_response(resp, cid)
        body = json_or_none(resp)
        order_id = None
        if isinstance(body, dict) and body.get("id") is not None:
            order_id = str(body["id"])
        return BrokerResult(order_id, resp.status_code, cid, dec_str(intent.quantity))
