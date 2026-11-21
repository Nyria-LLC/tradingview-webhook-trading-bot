"""Charles Schwab adapter built on schwab-py (an optional extra)."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any

import httpx

from .base import (
    Broker,
    BrokerRejected,
    BrokerResult,
    GuardRefused,
    NotConfigured,
    OrderIntent,
    OutcomeUnknown,
    dec_str,
    is_whole,
)

logger = logging.getLogger("tvbot")

ORDERS_URL = "https://api.schwabapi.com/trader/v1/accounts/{account}/orders"
ACCOUNT_PLACEHOLDER = "<SCHWAB_ACCOUNT_HASH>"
EXTRA_MESSAGE = 'install the Schwab extra: python -m pip install ".[schwab]"'
LOGIN_MESSAGE = "Schwab login expired or invalid; run python -m tvbot.schwab_login"
TOKEN_WARN_AGE_SECONDS = 6 * 86400
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _from_authlib(exc: BaseException) -> bool:
    """True when the exception class or a base class lives in schwab-py's OAuth library."""
    return any((getattr(klass, "__module__", "") or "").startswith("authlib") for klass in type(exc).__mro__)


def default_client_factory(settings: Any) -> Any:
    """Open a schwab-py client from the token file written by `python -m tvbot.schwab_login`."""
    path = settings.schwab_token_path
    if not os.path.exists(path):
        raise NotConfigured("run python -m tvbot.schwab_login first; SCHWAB_TOKEN_PATH was not found")
    try:
        from schwab.auth import client_from_token_file
    except ImportError:
        raise NotConfigured(EXTRA_MESSAGE) from None
    return client_from_token_file(path, settings.schwab_app_key, settings.schwab_app_secret)


class SchwabBroker(Broker):
    name = "schwab"
    required_env = ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_ACCOUNT_NUMBER")
    reads_positions = False
    uses_client_id = False

    def __init__(
        self,
        settings: Any,
        client_factory: Callable[[Any], Any] | None = None,
        *,
        scrubber: Any = None,
    ) -> None:
        super().__init__(settings, None, scrubber=scrubber)
        self._factory = client_factory or default_client_factory
        self._lock = threading.Lock()
        self._client: Any = None
        self._client_mtime: int | None = None
        self._account_hash: str | None = None

    # guards that run before any client exists

    def pre_check(self, intent: OrderIntent) -> None:
        if not self.settings.schwab_live:
            raise GuardRefused("Schwab orders from this bot are real orders; set SCHWAB_LIVE=true to place them")
        if intent.side == "sell" and not self.settings.schwab_allow_unchecked_sells:
            raise GuardRefused(
                "Schwab sells are off: this bot cannot check your Schwab position. "
                "Set SCHWAB_ALLOW_UNCHECKED_SELLS=true to send sells as Schwab SELL orders"
            )

    def validate(self, intent: OrderIntent) -> None:
        if not is_whole(intent.quantity):
            raise GuardRefused("Schwab orders must be whole shares in this bot")

    # request shape

    def _builder(self, intent: OrderIntent) -> Any:
        try:
            from schwab.orders.common import Duration
            from schwab.orders.equities import (
                equity_buy_limit,
                equity_buy_market,
                equity_sell_limit,
                equity_sell_market,
            )
        except ImportError:
            raise NotConfigured(EXTRA_MESSAGE) from None
        quantity = int(intent.quantity)
        symbol = intent.symbol
        if intent.order_type == "limit":
            price = dec_str(intent.limit_price)  # a string is sent unchanged; floats are truncated
            builder = (
                equity_buy_limit(symbol, quantity, price)
                if intent.side == "buy"
                else equity_sell_limit(symbol, quantity, price)
            )
        else:
            builder = (
                equity_buy_market(symbol, quantity)
                if intent.side == "buy"
                else equity_sell_market(symbol, quantity)
            )
        if intent.time_in_force == "gtc":
            builder = builder.set_duration(Duration.GOOD_TILL_CANCEL)
        return builder

    def build_request(self, intent: OrderIntent, cid: str | None, placeholders: bool) -> dict:
        # The account hash is never shown, so the placeholder is used in every case.
        return {
            "method": "POST",
            "url": ORDERS_URL.format(account=ACCOUNT_PLACEHOLDER),
            "json": self._builder(intent).build(),
        }

    # client and account hash, cached together with the token file's modification time

    def _token_mtime(self) -> int | None:
        try:
            return os.stat(self.settings.schwab_token_path).st_mtime_ns
        except OSError:
            return None

    def _drop_locked(self) -> None:
        self._client = None
        self._client_mtime = None
        self._account_hash = None

    def _drop(self) -> None:
        with self._lock:
            self._drop_locked()

    def _client_locked(self) -> Any:
        mtime = self._token_mtime()
        if self._client is not None and mtime != self._client_mtime:
            self._drop_locked()
        if self._client is None:
            self._client = self._factory(self.settings)
            self._client_mtime = mtime
        return self._client

    def _account_hash_locked(self, client: Any) -> str:
        if self._account_hash is not None:
            return self._account_hash
        try:
            resp = client.get_account_numbers()
        except Exception as exc:
            if _from_authlib(exc):
                self._drop_locked()
                raise BrokerRejected(None, LOGIN_MESSAGE) from None
            if isinstance(exc, httpx.HTTPError):
                raise BrokerRejected(None, f"Schwab request failed: {type(exc).__name__}") from None
            raise
        if resp.is_error:
            raise BrokerRejected(resp.status_code, resp.text)
        entries = resp.json()
        wanted = self.settings.schwab_account_number
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and str(entry.get("accountNumber")) == wanted:
                value = entry.get("hashValue")
                if not isinstance(value, str) or not value:
                    raise BrokerRejected(resp.status_code, "unexpected account response from Schwab")
                self.remember_secret(value)
                self._account_hash = value
                return value
        raise NotConfigured("SCHWAB_ACCOUNT_NUMBER is not one of the accounts this Schwab login can trade")

    # order phases

    def prepare(self, intent: OrderIntent, cid: str | None) -> tuple[Any, str, Any]:
        with self._lock:
            client = self._client_locked()
            account_hash = self._account_hash_locked(client)
        return client, account_hash, self._builder(intent)

    def send(self, prepared: tuple[Any, str, Any], intent: OrderIntent, cid: str | None) -> BrokerResult:
        client, account_hash, builder = prepared
        try:
            with self._lock:
                resp = client.place_order(account_hash, builder)
        except Exception as exc:
            if _from_authlib(exc):
                self._drop()
                raise BrokerRejected(None, LOGIN_MESSAGE) from None
            if isinstance(exc, _NOT_SENT):
                raise BrokerRejected(None, f"could not reach schwab: {type(exc).__name__}") from None
            raise OutcomeUnknown(None, type(exc).__name__) from None
        finally:
            self._warn_if_token_old(client)
        status = resp.status_code
        if status >= 500:
            raise OutcomeUnknown(None, f"Schwab returned HTTP {status}")
        if not 200 <= status < 300:
            raise BrokerRejected(status, resp.text)
        order_id = None
        try:
            from schwab.utils import Utils

            found = Utils(client, account_hash).extract_order_id(resp)
            order_id = None if found is None else str(found)
        except Exception:
            order_id = None  # successful responses carry the id only in a Location header
        return BrokerResult(order_id, status, None, dec_str(intent.quantity))

    # startup and token age

    def _warn_if_token_old(self, client: Any) -> None:
        try:
            age = client.token_age()
            if age > TOKEN_WARN_AGE_SECONDS:
                logger.warning(
                    "Schwab token is %d days old; run python -m tvbot.schwab_login before it expires at 7 days",
                    int(age // 86400),
                )
        except Exception:
            pass

    def warm_up(self) -> None:
        """Create the client once at startup; failures are logged and never stop the app."""
        try:
            with self._lock:
                client = self._client_locked()
            self._warn_if_token_old(client)
        except NotConfigured as exc:
            logger.warning("Schwab client not ready at startup: %s", exc)
        except Exception as exc:
            logger.warning("Schwab client not ready at startup: %s", type(exc).__name__)
