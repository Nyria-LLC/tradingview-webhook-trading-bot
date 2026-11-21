"""Shared test helpers: settings, a recording HTTP transport, fake brokers and app builders."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient

from tvbot.app import create_app
from tvbot.brokers.base import Broker, BrokerResult, NotConfigured, dec_str
from tvbot.config import load_settings

WEBHOOK_SECRET = "test-webhook-secret-0123456789abcdef"
ALPACA_KEY_ID = "fakealpacakeyid001"
ALPACA_SECRET = "fakealpacasecret002"
TRADIER_TOKEN = "faketradiertoken003"
TRADIER_ACCOUNT = "VA0000042"
TASTY_CLIENT_SECRET = "faketastysecret005"
TASTY_REFRESH = "faketastyrefresh006"
TASTY_ACCOUNT = "5WX99999"
SCHWAB_KEY = "fakeschwabkey007"
SCHWAB_SECRET = "fakeschwabsecret008"
SCHWAB_ACCOUNT = "987654321"

CREDENTIALS = {
    "ALPACA_API_KEY_ID": ALPACA_KEY_ID,
    "ALPACA_API_SECRET_KEY": ALPACA_SECRET,
    "TRADIER_ACCESS_TOKEN": TRADIER_TOKEN,
    "TRADIER_ACCOUNT_ID": TRADIER_ACCOUNT,
    "TASTYTRADE_CLIENT_SECRET": TASTY_CLIENT_SECRET,
    "TASTYTRADE_REFRESH_TOKEN": TASTY_REFRESH,
    "TASTYTRADE_ACCOUNT_NUMBER": TASTY_ACCOUNT,
    "SCHWAB_APP_KEY": SCHWAB_KEY,
    "SCHWAB_APP_SECRET": SCHWAB_SECRET,
    "SCHWAB_ACCOUNT_NUMBER": SCHWAB_ACCOUNT,
}
ALL_SECRETS = [WEBHOOK_SECRET, *CREDENTIALS.values()]


def make_env(**overrides: str) -> dict[str, str]:
    env = {"WEBHOOK_SECRET": WEBHOOK_SECRET, "LOG_LEVEL": "DEBUG", **CREDENTIALS}
    env.update(overrides)
    return env


def make_settings(**overrides: str):
    return load_settings(make_env(**overrides))


def alert(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"secret": WEBHOOK_SECRET, "symbol": "AAPL", "action": "buy", "quantity": 1}
    payload.update(overrides)
    return payload


def reply(status: int = 200, json: Any = None, text: str | None = None, headers: dict | None = None) -> dict:
    return {"status": status, "json": json, "text": text, "headers": headers or {}}


def raising(exc: Exception) -> dict:
    return {"exc": exc}


class Router:
    """MockTransport handler: canned replies per (method, path) and a record of every request.

    Several replies for one route are served in order and the last one repeats.
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], list[dict]] = {}
        self.requests: list[httpx.Request] = []
        self.unmatched: list[tuple[str, str]] = []

    def on(self, method: str, path: str, *replies: dict) -> "Router":
        self._routes[(method, path)] = list(replies)
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        replies = self._routes.get(key)
        if not replies:
            self.unmatched.append(key)
            return httpx.Response(599, json={"message": "no route"}, request=request)
        item = replies.pop(0) if len(replies) > 1 else replies[0]
        if "exc" in item:
            raise item["exc"]
        kwargs: dict[str, Any] = {"headers": item["headers"], "request": request}
        if item["json"] is not None:
            kwargs["json"] = item["json"]
        elif item["text"] is not None:
            kwargs["content"] = item["text"].encode()
        return httpx.Response(item["status"], **kwargs)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), timeout=5)

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


def body_json(request: httpx.Request) -> Any:
    return json.loads(request.content)


def body_form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBroker(Broker):
    """A broker with scriptable phases; place() is the shared Broker.place unless overridden."""

    name = "alpaca"
    required_env: tuple[str, ...] = ()
    reads_positions = False

    def __init__(self, settings: Any, *, on_prepare=None, on_send=None) -> None:
        super().__init__(settings, None)
        self.on_prepare = on_prepare
        self.on_send = on_send
        self.prepared = 0
        self.sent = 0
        self.intents: list[Any] = []

    def build_request(self, intent, cid, placeholders):
        return {"method": "POST", "url": "https://fake.invalid/orders", "json": {"symbol": intent.symbol}}

    def prepare(self, intent, cid):
        self.prepared += 1
        if self.on_prepare:
            return self.on_prepare(intent, cid)
        return {}

    def send(self, prepared, intent, cid):
        self.sent += 1
        self.intents.append(intent)
        if self.on_send:
            return self.on_send(prepared, intent, cid)
        return BrokerResult("fake-1", 200, cid, dec_str(intent.quantity))


class GatedBroker(FakeBroker):
    """Blocks inside send() until released, to hold one request in flight."""

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.entered = threading.Event()
        self.gate = threading.Event()
        self._mu = threading.Lock()
        self.active = 0
        self.max_active = 0

    def send(self, prepared, intent, cid):
        with self._mu:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.sent += 1
            self.intents.append(intent)
        self.entered.set()
        self.gate.wait(timeout=10)
        with self._mu:
            self.active -= 1
        return BrokerResult("fake-1", 200, cid, dec_str(intent.quantity))


class FakeSchwabClient:
    """Stands in for a schwab-py client; every answer is an httpx.Response like the real one."""

    def __init__(self, *, on_accounts=None, on_place=None, token_age: Any = 3600.0) -> None:
        self.on_accounts = on_accounts
        self.on_place = on_place
        self.age = token_age
        self.account_calls = 0
        self.placed: list[tuple[str, Any]] = []

    def get_account_numbers(self) -> httpx.Response:
        self.account_calls += 1
        if self.on_accounts is not None:
            return self.on_accounts()
        return httpx.Response(200, json=[{"accountNumber": SCHWAB_ACCOUNT, "hashValue": "HASHX"}])

    def place_order(self, account_hash: str, builder: Any) -> httpx.Response:
        self.placed.append((account_hash, builder))
        if self.on_place is not None:
            return self.on_place(account_hash, builder)
        location = f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}/orders/12345"
        return httpx.Response(201, headers={"Location": location})

    def token_age(self) -> float:
        if isinstance(self.age, BaseException):
            raise self.age
        return self.age


class CountingFactory:
    """A schwab client factory that counts its calls and hands out the given clients in turn."""

    def __init__(self, *clients: Any) -> None:
        self.clients = list(clients)
        self.calls = 0

    def __call__(self, settings: Any) -> Any:
        self.calls += 1
        if not self.clients:
            raise NotConfigured("no schwab client in this test")
        return self.clients.pop(0) if len(self.clients) > 1 else self.clients[0]


def build(settings=None, router: Router | None = None, **kwargs: Any):
    """Return (TestClient, app); the client turns server crashes into responses."""
    kwargs.setdefault("clock", FakeClock())
    kwargs.setdefault("schwab_client_factory", CountingFactory())
    if router is not None:
        kwargs.setdefault("http_client", router.client())
    client_address = kwargs.pop("client_address", ("testclient", 50000))
    app = create_app(settings or make_settings(), **kwargs)
    return TestClient(app, raise_server_exceptions=False, client=client_address), app


def build_live(router: Router | None = None, env: dict | None = None, **kwargs: Any):
    """A non-dry-run app; the duplicate guard is off unless the test sets DEDUPE_SECONDS."""
    values = {"DRY_RUN": "false", "DEDUPE_SECONDS": "0", **(env or {})}
    return build(make_settings(**values), router, **kwargs)


def all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    parts = [caplog.text]
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(str(record.exc_text))
        parts.append(str(record.exc_info))
    return "\n".join(parts)


def assert_no_secrets(caplog: pytest.LogCaptureFixture, *texts: str, secrets: list[str] | None = None) -> None:
    haystack = all_log_text(caplog) + "\n" + "\n".join(texts)
    for secret in secrets or ALL_SECRETS:
        assert secret not in haystack, f"secret value leaked: {secret[:4]}..."


@pytest.fixture(autouse=True)
def _tvbot_logger_state():
    """Remove filters a test attached to the tvbot logger."""
    logger = logging.getLogger("tvbot")
    before = list(logger.filters)
    yield
    for added in [f for f in logger.filters if f not in before]:
        logger.removeFilter(added)
