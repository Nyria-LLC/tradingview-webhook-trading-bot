"""Order intent, result and error types plus the Broker base class shared by every adapter."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import httpx

logger = logging.getLogger("tvbot")


def dec_str(d: Decimal) -> str:
    """Plain (never exponent) decimal text."""
    return format(d, "f")


def is_whole(d: Decimal) -> bool:
    return d == d.to_integral_value()


def client_id() -> str:
    """Order tag sent to the broker: letters, digits and a hyphen only."""
    return "tvbot-" + uuid.uuid4().hex[:24]


@dataclass(frozen=True)
class OrderIntent:
    broker: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: str
    limit_price: Decimal | None
    time_in_force: str


@dataclass(frozen=True)
class BrokerResult:
    order_id: str | None
    status_code: int | None
    client_id: str | None
    quantity: str


class GuardRefused(Exception):
    """A safety rule of this bot refused the order (HTTP 422)."""


class NotConfigured(Exception):
    """The broker cannot be used until configuration or an extra is installed (HTTP 422)."""


class BrokerRejected(Exception):
    """The broker refused the order or nothing was sent (HTTP 424)."""

    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class OutcomeUnknown(Exception):
    """An order request may have reached the broker (HTTP 504)."""

    def __init__(self, client_id: str | None, reason: str) -> None:
        super().__init__(reason)
        self.client_id = client_id
        self.reason = reason


_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


class Broker:
    """One broker adapter. Subclasses set the class attributes and implement the phases."""

    name = ""
    required_env: tuple[str, ...] = ()
    reads_positions = False
    uses_client_id = True

    def __init__(self, settings: Any, http_client: Any = None, *, scrubber: Any = None) -> None:
        self.settings = settings
        self.http = http_client
        self.scrubber = scrubber

    # configuration

    def missing_env(self) -> list[str]:
        return [var for var in self.required_env if not getattr(self.settings, var.lower())]

    def remember_secret(self, value: str | None) -> None:
        """Register a value obtained at runtime so logs and responses never show it."""
        if self.scrubber is not None and value:
            self.scrubber.add(value)

    # transport

    def _send(
        self, method: str, url: str, *, is_order: bool, cid: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        """One HTTP call. Transport errors are classified by class name only."""
        try:
            return self.http.request(method, url, **kwargs)
        except _NOT_SENT as exc:
            raise BrokerRejected(None, f"could not reach {self.name}: {type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            if is_order:
                raise OutcomeUnknown(cid, type(exc).__name__) from None
            raise BrokerRejected(None, f"{self.name} request failed: {type(exc).__name__}") from None

    def order_response(self, resp: httpx.Response, cid: str | None) -> None:
        """Raise for a non-2xx order response: 5xx is unknown, anything else a refusal."""
        status = resp.status_code
        if 200 <= status < 300:
            return
        if status >= 500:
            raise OutcomeUnknown(cid, f"{self.name} returned HTTP {status}")
        raise BrokerRejected(status, self.error_message(resp))

    def error_message(self, resp: httpx.Response) -> str:
        return resp.text

    # phases implemented per broker

    def validate(self, intent: OrderIntent) -> None:
        """Raise GuardRefused for an intent this broker cannot take."""

    def build_request(self, intent: OrderIntent, cid: str | None, placeholders: bool) -> dict:
        raise NotImplementedError

    def preview(self, intent: OrderIntent) -> dict:
        return self.build_request(intent, client_id(), placeholders=True)

    def pre_check(self, intent: OrderIntent) -> None:
        """Checks that run before any client creation or network call."""

    def held_quantity(self, symbol: str) -> Decimal:
        raise NotImplementedError

    def prepare(self, intent: OrderIntent, cid: str | None) -> Any:
        raise NotImplementedError

    def send(self, prepared: Any, intent: OrderIntent, cid: str | None) -> BrokerResult:
        raise NotImplementedError

    # the order flow

    def place(self, intent: OrderIntent, clamp_sells: bool) -> BrokerResult:
        try:
            self.pre_check(intent)
            if intent.side == "sell" and self.reads_positions:
                intent = self._check_sell(intent, clamp_sells)
        except (GuardRefused, NotConfigured, BrokerRejected, OutcomeUnknown):
            raise
        except Exception as exc:
            raise BrokerRejected(None, f"{self.name} could not check the position: {type(exc).__name__}") from None

        cid = client_id() if self.uses_client_id else None
        try:
            prepared = self.prepare(intent, cid)
        except (GuardRefused, NotConfigured, BrokerRejected, OutcomeUnknown):
            raise
        except Exception as exc:
            raise BrokerRejected(None, f"{self.name} could not prepare the order: {type(exc).__name__}") from None

        try:
            return self.send(prepared, intent, cid)
        except (BrokerRejected, OutcomeUnknown):
            raise
        except Exception as exc:
            raise OutcomeUnknown(cid, type(exc).__name__) from None

    def _check_sell(self, intent: OrderIntent, clamp_sells: bool) -> OrderIntent:
        """Keep the bot long-only: refuse, or with clamp_sells trim, a sell above the long position."""
        symbol = intent.symbol
        held = self.held_quantity(symbol)
        if held <= 0:
            raise GuardRefused(f"no long position in {symbol} to sell; this bot does not open short positions")
        held = held.normalize()  # a JSON 13.0 reads as 13 in the messages and in a clamped order
        if intent.quantity > held:
            if not clamp_sells:
                raise GuardRefused(f"sell quantity {dec_str(intent.quantity)} exceeds the {dec_str(held)} shares held in {symbol}")
            logger.warning("sell clamped from %s to %s for %s", dec_str(intent.quantity), dec_str(held), symbol)
            intent = replace(intent, quantity=held)
            self.validate(intent)
        return intent
