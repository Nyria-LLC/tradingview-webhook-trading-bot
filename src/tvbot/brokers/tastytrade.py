"""tastytrade API adapter (plain HTTP, OAuth refresh-token grant, no SDK)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from .. import __version__
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

LIVE_BASE = "https://api.tastyworks.com"
SANDBOX_BASE = "https://api.cert.tastyworks.com"
ACCOUNT_PLACEHOLDER = "<TASTYTRADE_ACCOUNT_NUMBER>"
USER_AGENT = f"tradingview-webhook-trading-bot/{__version__}"
SOURCE = "tradingview-webhook-trading-bot"
DEFAULT_TOKEN_SECONDS = 900  # access tokens are valid for 15 minutes
TOKEN_MARGIN_SECONDS = 60


class TastytradeBroker(Broker):
    name = "tastytrade"
    required_env = ("TASTYTRADE_CLIENT_SECRET", "TASTYTRADE_REFRESH_TOKEN", "TASTYTRADE_ACCOUNT_NUMBER")
    reads_positions = True

    def __init__(
        self,
        settings: Any,
        http_client: Any,
        *,
        scrubber: Any = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(settings, http_client, scrubber=scrubber)
        self._clock = clock or time.monotonic
        self._token_lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at = 0.0

    def _base(self) -> str:
        return LIVE_BASE if self.settings.tastytrade_live else SANDBOX_BASE

    def _account(self, placeholders: bool) -> str:
        if placeholders:
            return ACCOUNT_PLACEHOLDER
        return quote(self.settings.tastytrade_account_number, safe="")

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"User-Agent": USER_AGENT, "Accept": "application/json"}

    # tokens

    def _token(self) -> str:
        with self._token_lock:
            if self._access_token is not None and self._clock() < self._expires_at:
                return self._access_token
            return self._fetch_token()

    def _refresh_after_401(self, stale: str) -> str:
        """Replace the token that drew a 401, unless another thread already did."""
        with self._token_lock:
            if self._access_token is not None and self._access_token != stale:
                return self._access_token
            self._access_token = None
            return self._fetch_token()

    def _fetch_token(self) -> str:
        """Call the token endpoint; the caller holds the token lock."""
        resp = self._send(
            "POST",
            f"{self._base()}/oauth/token",
            is_order=False,
            headers=self._headers(),
            json={
                "grant_type": "refresh_token",
                "refresh_token": self.settings.tastytrade_refresh_token,
                "client_secret": self.settings.tastytrade_client_secret,
            },
        )
        if not 200 <= resp.status_code < 300:
            raise BrokerRejected(resp.status_code, self.error_message(resp))
        body = json_or_none(resp)
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise BrokerRejected(resp.status_code, "tastytrade token response had no access_token")
        self.remember_secret(token)
        try:
            lifetime = float(body.get("expires_in", DEFAULT_TOKEN_SECONDS))
        except (TypeError, ValueError):
            lifetime = float(DEFAULT_TOKEN_SECONDS)
        self._access_token = token
        self._expires_at = self._clock() + lifetime - TOKEN_MARGIN_SECONDS
        return token

    def _call(self, method: str, url: str, *, is_order: bool, cid: str | None = None, **kwargs: Any) -> Any:
        """An authorised call; one 401 drops the token and retries that call once."""
        token = self._token()
        resp = self._send(
            method, url, is_order=is_order, cid=cid, headers={**self._headers(), "Authorization": f"Bearer {token}"}, **kwargs
        )
        if resp.status_code == 401:
            token = self._refresh_after_401(token)
            resp = self._send(
                method,
                url,
                is_order=is_order,
                cid=cid,
                headers={**self._headers(), "Authorization": f"Bearer {token}"},
                **kwargs,
            )
        return resp

    def error_message(self, resp: Any) -> str:
        body = json_or_none(resp)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
            message = error.get("message") if isinstance(error.get("message"), str) else ""
            errors = error.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                detail = errors[0].get("message")
                if isinstance(detail, str) and detail:
                    message = f"{message}; {detail}" if message else detail
            if message:
                return message
        return resp.text

    # order phases

    def validate(self, intent: OrderIntent) -> None:
        if not is_whole(intent.quantity):
            raise GuardRefused("tastytrade orders must be whole shares in this bot")
        if intent.order_type == "market" and intent.time_in_force == "gtc":
            raise GuardRefused("tastytrade market orders must be day orders")

    def build_request(self, intent: OrderIntent, cid: str | None, placeholders: bool) -> dict:
        body = {
            "time-in-force": "GTC" if intent.time_in_force == "gtc" else "Day",
            "order-type": "Limit" if intent.order_type == "limit" else "Market",
            "source": SOURCE,
            "external-identifier": cid,
            "legs": [
                {
                    "instrument-type": "Equity",
                    "symbol": intent.symbol,
                    "quantity": str(int(intent.quantity)),
                    "action": "Buy to Open" if intent.side == "buy" else "Sell to Close",
                }
            ],
        }
        if intent.order_type == "limit":
            body["price"] = dec_str(intent.limit_price)
            body["price-effect"] = "Debit" if intent.side == "buy" else "Credit"
        url = f"{self._base()}/accounts/{self._account(placeholders)}/orders"
        return {"method": "POST", "url": url, "json": body}

    def held_quantity(self, symbol: str) -> Decimal:
        url = f"{self._base()}/accounts/{self._account(False)}/positions"
        resp = self._call("GET", url, is_order=False, params={"symbol": symbol})
        if not 200 <= resp.status_code < 300:
            raise BrokerRejected(resp.status_code, self.error_message(resp))
        body = json_or_none(resp)
        data = body.get("data") if isinstance(body, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return Decimal(0)
        for item in items:
            if (
                isinstance(item, dict)
                and item.get("symbol") == symbol
                and item.get("instrument-type") == "Equity"
                and item.get("quantity-direction") == "Long"
            ):
                try:
                    held = Decimal(str(item["quantity"]))
                except (InvalidOperation, KeyError):
                    raise BrokerRejected(resp.status_code, "unexpected position quantity from tastytrade") from None
                if not held.is_finite():
                    raise BrokerRejected(resp.status_code, "unexpected position quantity from tastytrade")
                return held
        return Decimal(0)

    def prepare(self, intent: OrderIntent, cid: str | None) -> dict:
        self._token()  # a token failure is a refusal before any order request goes out
        return self.build_request(intent, cid, placeholders=False)

    def send(self, prepared: dict, intent: OrderIntent, cid: str | None) -> BrokerResult:
        resp = self._call("POST", prepared["url"], is_order=True, cid=cid, json=prepared["json"])
        self.order_response(resp, cid)
        order_id = None
        body = json_or_none(resp)
        data = body.get("data") if isinstance(body, dict) else None
        order = data.get("order") if isinstance(data, dict) else None
        if isinstance(order, dict) and order.get("id") is not None:
            order_id = str(order["id"])
        return BrokerResult(order_id, resp.status_code, cid, dec_str(intent.quantity))
