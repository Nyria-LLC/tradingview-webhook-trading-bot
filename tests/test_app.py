"""The webhook route: status paths, dry run, the duplicate guard, secret hygiene and the catch-all."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
import time
from decimal import Decimal

import httpx
import pytest
from conftest import (
    ALL_SECRETS,
    SCHWAB_ACCOUNT,
    TASTY_ACCOUNT,
    TASTY_CLIENT_SECRET,
    TRADIER_ACCOUNT,
    TRADIER_TOKEN,
    WEBHOOK_SECRET,
    CountingFactory,
    FakeBroker,
    FakeClock,
    FakeSchwabClient,
    GatedBroker,
    Router,
    alert,
    all_log_text,
    assert_no_secrets,
    build,
    build_live,
    make_env,
    make_settings,
    raising,
    reply,
)
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tvbot import __version__
from tvbot.app import MAX_BODY_BYTES, REAL_ORDER_WARNING, UNEXPECTED, CatchAllMiddleware, create_app
from tvbot.brokers.base import (
    BrokerRejected,
    BrokerResult,
    GuardRefused,
    NotConfigured,
    OutcomeUnknown,
    dec_str,
)
from tvbot.config import ConfigError, load_settings

CID = re.compile(r"tvbot-[0-9a-f]{24}")


def post(client, payload=None, **kwargs):
    if payload is not None:
        kwargs["json"] = payload
    return client.post("/webhook", **kwargs)


def fake_app(factory=FakeBroker, **env):
    """A live (DRY_RUN false) app whose alpaca broker is replaced by a scriptable one."""
    settings = make_settings(DRY_RUN="false", **env)
    client, app = build(settings, Router())
    broker = factory(settings)
    app.state.brokers["alpaca"] = broker
    return client, app, broker


def tvbot_records(caplog):
    return [r for r in caplog.records if r.name == "tvbot"]


def asgi_request(app, chunks, *, headers=None, client=("203.0.113.5", 40000), method="POST", path="/webhook"):
    """Drive the ASGI app directly: returns (status, body, number of receive() calls)."""
    sent: list[dict] = []
    queue = list(chunks)
    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        if queue:
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    raw_headers = [
        (name.lower().encode("latin-1"), value if isinstance(value, bytes) else value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": client,
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body), reads


# static routes


def test_health():
    client, _ = build()
    response = client.get("/health")
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert response.headers["content-type"].startswith("application/json")


def test_unknown_path_is_a_json_404():
    client, _ = build()
    for response in (client.get("/nope"), client.post("/nope", json={})):
        assert response.status_code == 404
        assert response.json() == {"ok": False, "error": "not found"}


def test_wrong_method_is_a_json_405_with_an_allow_header():
    client, _ = build()
    response = client.get("/webhook")
    assert response.status_code == 405
    assert response.json() == {"ok": False, "error": "method not allowed"}
    assert response.headers["allow"] == "POST"
    for other in (client.put("/webhook"), client.delete("/webhook"), client.post("/health")):
        assert other.status_code == 405
        assert other.json() == {"ok": False, "error": "method not allowed"}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_interactive_docs_are_off(path):
    client, _ = build()
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "not found"}


def test_other_http_exceptions_use_the_same_json_shape():
    client, app = build()

    @app.get("/teapot")
    async def teapot():
        raise HTTPException(status_code=418, detail="short and stout", headers={"X-Test": "1"})

    response = client.get("/teapot")
    assert response.status_code == 418
    assert response.json() == {"ok": False, "error": "short and stout"}
    assert response.headers["x-test"] == "1"


# the caller gate (403)


def test_client_outside_the_ip_list_is_forbidden_before_the_body_is_read():
    settings = make_settings(TRADINGVIEW_IPS_ONLY="true", TRADINGVIEW_IPS="52.89.214.238")
    client, app = build(settings, client_address=("203.0.113.9", 1234))
    for response in (
        post(client, alert()),
        post(client, content=b"garbage that is not json"),
        post(client, alert(), headers={"X-Forwarded-For": "52.89.214.238"}),
    ):
        assert response.status_code == 403
        assert response.json() == {"ok": False, "error": "forbidden"}


def test_client_inside_the_ip_list_passes():
    settings = make_settings(TRADINGVIEW_IPS_ONLY="true", TRADINGVIEW_IPS="34.212.75.30, 52.89.214.238")
    client, _ = build(settings, client_address=("52.89.214.238", 4321))
    response = post(client, alert())
    assert response.status_code == 200
    assert response.json()["dry_run"] is True


def test_default_list_is_used_when_ips_only_is_on_without_a_list():
    settings = make_settings(TRADINGVIEW_IPS_ONLY="true")
    inside, _ = build(settings, client_address=("54.218.53.128", 1))
    outside, _ = build(settings)  # the TestClient's own host name is not in the list
    assert post(inside, alert()).status_code == 200
    assert post(outside, alert()).status_code == 403


def test_a_request_without_a_client_address_is_forbidden_when_ips_only_is_on():
    _, app = build(make_settings(TRADINGVIEW_IPS_ONLY="true"))
    status, body, reads = asgi_request(app, [b"{}"], client=None)
    assert (status, body) == (403, {"ok": False, "error": "forbidden"})
    assert reads == 0


def test_any_host_may_call_when_ips_only_is_off():
    client, _ = build(client_address=("203.0.113.9", 1234))
    assert post(client, alert()).status_code == 200


def test_forbidden_call_is_logged_with_the_host(caplog):
    caplog.set_level(logging.DEBUG)
    settings = make_settings(TRADINGVIEW_IPS_ONLY="true")
    client, _ = build(settings, client_address=("203.0.113.9", 1234))
    post(client, alert())
    assert any("webhook refused status=403: client host 203.0.113.9" in r.getMessage() for r in tvbot_records(caplog))


# the size gate (413)


def test_413_by_content_length():
    client, _ = build()
    response = post(client, content=b" " * 20000)
    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "body too large"}


def test_413_by_content_length_without_reading_the_body():
    _, app = build()
    status, body, reads = asgi_request(app, [b"{}"], headers={"content-length": "20000"})
    assert (status, body) == (413, {"ok": False, "error": "body too large"})
    assert reads == 0


def test_413_for_a_streamed_20_kb_body_without_content_length():
    client, _ = build()

    def chunks():
        for _ in range(4):
            yield b" " * 5000

    response = post(client, content=chunks())
    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "body too large"}


def test_the_stream_is_cut_off_as_soon_as_the_limit_is_passed():
    _, app = build()
    status, body, reads = asgi_request(app, [b" " * 5000] * 5)  # no content-length header
    assert (status, body) == (413, {"ok": False, "error": "body too large"})
    assert reads == 3  # 5000, 10000, then 15000 > 10240


def test_a_body_of_exactly_the_limit_is_accepted_and_one_byte_more_is_not():
    client, _ = build()
    base = json.dumps(alert()).encode()
    exact = base + b" " * (MAX_BODY_BYTES - len(base))
    assert len(exact) == MAX_BODY_BYTES == 10240
    assert post(client, content=exact).status_code == 200
    assert post(client, content=exact + b" ").status_code == 413


@pytest.mark.parametrize("declared", [b"abc", b"\xb9", b"-5", b"", b"1e3", b"  "])
def test_a_malformed_content_length_header_cannot_crash_the_route(declared):
    _, app = build()
    status, body, _reads = asgi_request(app, [json.dumps(alert()).encode()], headers={"content-length": declared})
    assert status == 200 and body["dry_run"] is True


# the body (400)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"hello",
        b"{",
        b'{"a": 1,}',
        b"\xff\xfe\x00{",
        b"\xef\xbb\xbf" + json.dumps(alert()).encode(),
        json.dumps(alert()).encode("utf-16"),
        json.dumps(alert()).encode("utf-16-le"),
        b"[1, 2, 3]",
        json.dumps([alert()]).encode(),
        b'"just a string"',
        b"123",
        b"null",
        b"true",
        b"[" * 5000 + b"]" * 5000,
        b"[" * MAX_BODY_BYTES,
        b'{"secret": "x", "quantity": ' + b"1" * 5000 + b"}",
    ],
    ids=lambda b: f"{len(b)}-bytes-{b[:12]!r}",
)
def test_a_body_that_is_not_a_json_object_is_a_400(body):
    client, _ = build()
    response = post(client, content=body)
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "body must be a JSON object"}


def test_content_type_is_not_checked():
    client, _ = build()
    text = json.dumps(alert())
    assert post(client, content=text, headers={"content-type": "text/plain"}).status_code == 200
    assert post(client, content=text, headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 200
    assert post(client, content=text.encode()).status_code == 200


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999", "-0"])
def test_non_finite_or_zero_quantity_literals_are_a_422(literal):
    client, _ = build()
    body = (
        f'{{"secret": "{WEBHOOK_SECRET}", "symbol": "AAPL", "action": "buy", "quantity": {literal}}}'
    )
    response = post(client, content=body)
    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "quantity must be a positive number"}


# the secret (401) comes before any field rule


def test_wrong_secret_is_checked_before_every_field():
    client, _ = build()
    response = post(client, {"secret": "wrong", "symbol": "!!", "action": "hold", "quantity": -5, "broker": "nope"})
    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "unauthorized"}


def test_the_same_body_with_the_right_secret_reaches_the_field_rules():
    client, _ = build()
    response = post(client, {"secret": WEBHOOK_SECRET, "symbol": "!!", "action": "hold", "quantity": -5})
    assert response.status_code == 422


@pytest.mark.parametrize("supplied", [None, 5, [], {}, "", "wrong", WEBHOOK_SECRET + " ", WEBHOOK_SECRET.upper()])
def test_bad_secrets_are_a_401(supplied):
    client, _ = build()
    response = post(client, alert(secret=supplied))
    assert (response.status_code, response.json()) == (401, {"ok": False, "error": "unauthorized"})


def test_missing_secret_is_a_401():
    client, _ = build()
    body = alert()
    del body["secret"]
    assert post(client, body).status_code == 401


def test_a_lone_surrogate_secret_is_a_401_not_a_crash():
    client, _ = build()
    response = post(client, content=b'{"secret": "\\ud800", "symbol": "AAPL", "action": "buy", "quantity": 1}')
    assert response.status_code == 401


def test_a_refused_secret_never_reaches_the_broker():
    client, _, broker = fake_app()
    assert post(client, alert(secret="wrong")).status_code == 401
    assert (broker.prepared, broker.sent) == (0, 0)


# field refusals (422)


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"symbol": "!!"}, "invalid symbol"),
        ({"action": "hold"}, "action must be buy or sell"),
        ({"quantity": 0}, "quantity must be a positive number"),
        ({"quantity": 1000}, "quantity exceeds MAX_QTY"),
        ({"broker": "x"}, "unknown broker"),
        ({"order_type": "stop"}, "order_type must be market or limit"),
        ({"order_type": "limit"}, "limit_price is required for limit orders"),
        ({"time_in_force": "ioc"}, "time_in_force must be day or gtc"),
    ],
)
def test_field_refusals_are_a_422_with_the_message(overrides, message):
    client, _ = build()
    response = post(client, alert(**overrides))
    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": message}


def test_symbol_outside_allowed_symbols_is_a_422():
    client, _ = build(make_settings(ALLOWED_SYMBOLS="MSFT"))
    response = post(client, alert(symbol="AAPL"))
    assert (response.status_code, response.json()["error"]) == (422, "symbol not allowed")


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"broker": "alpaca", "quantity": 1.5, "order_type": "limit", "limit_price": 10}, "Alpaca accepts fractional quantities only for market day orders"),
        ({"broker": "tradier", "quantity": 1.5}, "Tradier orders must be whole shares"),
        ({"broker": "tastytrade", "quantity": 1.5}, "tastytrade orders must be whole shares in this bot"),
        ({"broker": "tastytrade", "time_in_force": "gtc"}, "tastytrade market orders must be day orders"),
        ({"broker": "schwab", "quantity": 1.5}, "Schwab orders must be whole shares in this bot"),
    ],
)
def test_broker_rules_are_checked_before_the_dry_run_preview(overrides, message):
    client, _ = build()
    response = post(client, alert(**overrides))
    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": message}


@pytest.mark.parametrize(
    "broker,blank,message",
    [
        ("alpaca", ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"], "alpaca is not configured: set ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY"),
        ("alpaca", ["ALPACA_API_SECRET_KEY"], "alpaca is not configured: set ALPACA_API_SECRET_KEY"),
        ("tradier", ["TRADIER_ACCESS_TOKEN", "TRADIER_ACCOUNT_ID"], "tradier is not configured: set TRADIER_ACCESS_TOKEN, TRADIER_ACCOUNT_ID"),
        (
            "tastytrade",
            ["TASTYTRADE_CLIENT_SECRET", "TASTYTRADE_REFRESH_TOKEN", "TASTYTRADE_ACCOUNT_NUMBER"],
            "tastytrade is not configured: set TASTYTRADE_CLIENT_SECRET, TASTYTRADE_REFRESH_TOKEN, TASTYTRADE_ACCOUNT_NUMBER",
        ),
        (
            "schwab",
            ["SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_ACCOUNT_NUMBER"],
            "schwab is not configured: set SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_ACCOUNT_NUMBER",
        ),
        ("schwab", ["SCHWAB_APP_SECRET"], "schwab is not configured: set SCHWAB_APP_SECRET"),
    ],
)
def test_a_broker_without_its_credentials_is_a_422_that_names_the_missing_variables(broker, blank, message):
    router = Router()
    client, _ = build_live(router, {name: "" for name in blank} | {"SCHWAB_LIVE": "true", "DEDUPE_SECONDS": "30"})
    first = post(client, alert(broker=broker))
    assert first.status_code == 422
    assert first.json() == {"ok": False, "error": message}
    assert router.requests == []
    assert post(client, alert(broker=broker)).json() == first.json()  # the entry was dropped, not remembered


def test_dry_run_needs_no_credentials():
    router = Router()
    blank = {name: "" for name in ("TRADIER_ACCESS_TOKEN", "TRADIER_ACCOUNT_ID")}
    client, _ = build(make_settings(**blank), router)
    response = post(client, alert(broker="tradier"))
    assert response.status_code == 200
    assert "<TRADIER_ACCOUNT_ID>" in response.json()["request"]["url"]
    assert router.requests == []


# dry run


@pytest.mark.parametrize("broker", ["alpaca", "tradier", "tastytrade", "schwab"])
def test_dry_run_is_the_default_and_makes_no_transport_call(broker):
    settings = load_settings(make_env())
    assert settings.dry_run is True
    router = Router()
    client, _ = build(settings, router)
    response = post(client, alert(broker=broker))
    body = response.json()
    assert response.status_code == 200
    assert (body["ok"], body["dry_run"], body["broker"]) == (True, True, broker)
    assert body["request"]["method"] == "POST"
    assert router.requests == [] and router.unmatched == []


@pytest.mark.parametrize(
    "broker,placeholder",
    [
        ("alpaca", None),
        ("tradier", "<TRADIER_ACCOUNT_ID>"),
        ("tastytrade", "<TASTYTRADE_ACCOUNT_NUMBER>"),
        ("schwab", "<SCHWAB_ACCOUNT_HASH>"),
    ],
)
def test_dry_run_previews_show_placeholders_and_never_a_configured_value(broker, placeholder):
    client, _ = build()
    response = post(client, alert(broker=broker, action="sell", quantity=3))
    if placeholder:
        assert placeholder in response.json()["request"]["url"]
    for secret in ALL_SECRETS:
        assert secret not in response.text


def test_dry_run_preview_carries_a_fresh_client_id():
    client, _ = build()
    first = post(client, alert()).json()["request"]["json"]["client_order_id"]
    second = post(client, alert()).json()["request"]["json"]["client_order_id"]
    assert CID.fullmatch(first) and CID.fullmatch(second) and first != second


def test_dry_run_does_not_read_positions_for_a_sell():
    router = Router()
    client, _ = build(make_settings(), router)
    response = post(client, alert(action="sell"))
    assert response.status_code == 200 and router.requests == []


def test_dry_run_has_no_duplicate_guard():
    client, _ = build()
    bodies = [post(client, alert()).json() for _ in range(3)]
    assert all(b["ok"] is True and b["dry_run"] is True and "duplicate" not in b for b in bodies)


def test_dry_run_reports_a_missing_schwab_extra_as_a_422(monkeypatch):
    monkeypatch.setitem(sys.modules, "schwab.orders.common", None)
    client, _ = build()
    response = post(client, alert(broker="schwab"))
    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": 'install the Schwab extra: python -m pip install ".[schwab]"'}


def test_startup_line_in_dry_run(caplog):
    caplog.set_level(logging.DEBUG)
    build()
    messages = [r.getMessage() for r in tvbot_records(caplog)]
    assert f"tvbot {__version__} dry_run=True default_broker=alpaca live=none" in messages
    assert REAL_ORDER_WARNING not in messages


def test_startup_line_and_warning_when_orders_are_real(caplog):
    caplog.set_level(logging.DEBUG)
    settings = make_settings(DRY_RUN="false", DEFAULT_BROKER="tradier", ALPACA_LIVE="true", SCHWAB_LIVE="true")
    build(settings)
    records = tvbot_records(caplog)
    messages = [r.getMessage() for r in records]
    assert f"tvbot {__version__} dry_run=False default_broker=tradier live=alpaca,schwab" in messages
    warning = [r for r in records if r.getMessage() == REAL_ORDER_WARNING]
    assert len(warning) == 1 and warning[0].levelno == logging.WARNING
    assert REAL_ORDER_WARNING == (
        "order requests follow each broker's published API; no order has been placed through this bot "
        "by its authors. Start with DRY_RUN=true and a paper or sandbox account."
    )


def test_dry_run_log_line(caplog):
    caplog.set_level(logging.DEBUG)
    client, _ = build()
    post(client, alert())
    last = [r for r in tvbot_records(caplog) if r.levelno == logging.INFO][-1]
    assert last.getMessage() == (
        "alert broker=alpaca symbol=AAPL action=buy qty=1 type=market tif=day dry_run=True "
        "status=200 order_id=- client_id=-"
    )


# live orders through a scriptable broker


def test_live_success_body_and_log_line(caplog):
    caplog.set_level(logging.DEBUG)
    client, _, broker = fake_app()
    response = post(client, alert(quantity=2))
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"ok", "dry_run", "broker", "order_id", "client_id", "quantity"}
    assert (body["ok"], body["dry_run"], body["broker"], body["order_id"], body["quantity"]) == (
        True,
        False,
        "alpaca",
        "fake-1",
        "2",
    )
    assert CID.fullmatch(body["client_id"])
    assert (broker.prepared, broker.sent) == (1, 1)
    last = [r for r in tvbot_records(caplog) if r.levelno == logging.INFO][-1].getMessage()
    assert re.fullmatch(
        r"alert broker=alpaca symbol=AAPL action=buy qty=2 type=market tif=day dry_run=False "
        r"status=200 order_id=fake-1 client_id=tvbot-[0-9a-f]{24}",
        last,
    )


def test_the_order_reaches_the_broker_as_parsed():
    client, _, broker = fake_app()
    post(client, alert(symbol="nasdaq:aapl", action="SELL", quantity="2.5", order_type="limit", limit_price="10.25", time_in_force="gtc"))
    [intent] = broker.intents
    assert (intent.broker, intent.symbol, intent.side, dec_str(intent.quantity)) == ("alpaca", "AAPL", "sell", "2.5")
    assert (intent.order_type, dec_str(intent.limit_price), intent.time_in_force) == ("limit", "10.25", "gtc")


def _prepare_raises(exc):
    def hook(intent, cid):
        raise exc

    return {"on_prepare": hook}


def _send_raises(exc):
    def hook(prepared, intent, cid):
        raise exc

    return {"on_send": hook}


LONG_MESSAGE = "y" * 1000


@pytest.mark.parametrize(
    "hooks,status,body",
    [
        (_prepare_raises(GuardRefused("guard says no")), 422, {"ok": False, "error": "guard says no"}),
        (_prepare_raises(NotConfigured("not ready")), 422, {"ok": False, "error": "not ready"}),
        (
            _prepare_raises(BrokerRejected(403, "insufficient buying power")),
            424,
            {"ok": False, "error": "insufficient buying power", "broker_status": 403},
        ),
        (
            _send_raises(BrokerRejected(None, "could not reach alpaca: ConnectError")),
            424,
            {"ok": False, "error": "could not reach alpaca: ConnectError", "broker_status": None},
        ),
        (
            _prepare_raises(RuntimeError("anything")),
            424,
            {"ok": False, "error": "alpaca could not prepare the order: RuntimeError", "broker_status": None},
        ),
        (
            _prepare_raises(BrokerRejected(400, LONG_MESSAGE)),
            424,
            {"ok": False, "error": "y" * 300, "broker_status": 400},
        ),
    ],
    ids=["guard", "not-configured", "rejected-403", "rejected-no-status", "prepare-crash", "long-message"],
)
def test_refusals_map_to_422_or_424_and_are_not_remembered(hooks, status, body):
    client, _, broker = fake_app(lambda s: FakeBroker(s, **hooks))
    first = post(client, alert())
    assert (first.status_code, first.json()) == (status, body)
    second = post(client, alert())
    assert (second.status_code, second.json()) == (status, body)  # not answered as a duplicate
    assert broker.prepared == 2


def test_outcome_unknown_is_a_504_that_names_the_client_id_and_is_remembered():
    client, _, broker = fake_app(lambda s: FakeBroker(s, **_send_raises(OutcomeUnknown("tvbot-abc", "ReadTimeout"))))
    first = post(client, alert())
    assert first.status_code == 504
    assert first.json() == {
        "ok": False,
        "error": "outcome unknown (ReadTimeout); the order may have been placed. Check your broker for client id tvbot-abc",
    }
    assert post(client, alert()).json() == {"ok": True, "duplicate": True}
    assert broker.sent == 1


def test_outcome_unknown_without_a_client_id_names_the_broker():
    client, _, _ = fake_app(lambda s: FakeBroker(s, **_send_raises(OutcomeUnknown(None, "ReadTimeout"))))
    response = post(client, alert())
    assert response.status_code == 504
    assert response.json()["error"] == (
        "outcome unknown (ReadTimeout); the order may have been placed. Check your Alpaca orders."
    )


def test_an_unexpected_error_inside_the_broker_call_is_a_504_with_the_real_client_id():
    seen: list[str] = []

    def hook(prepared, intent, cid):
        seen.append(cid)
        raise RuntimeError("socket exploded")

    client, _, _ = fake_app(lambda s: FakeBroker(s, on_send=hook))
    response = post(client, alert())
    assert response.status_code == 504
    assert response.json()["error"] == (
        f"outcome unknown (RuntimeError); the order may have been placed. Check your broker for client id {seen[0]}"
    )


class PlaceBreaks(FakeBroker):
    """place() itself raises, which is the route's own last line of defence."""

    def place(self, intent, clamp_sells):
        raise RuntimeError("place broke")


def test_an_unexpected_error_after_place_started_is_a_504_and_remembered():
    client, _, _ = fake_app(PlaceBreaks)
    first = post(client, alert())
    assert first.status_code == 504
    assert first.json() == {
        "ok": False,
        "error": "outcome unknown (unexpected error); the order may have been placed. Check your Alpaca orders.",
    }
    assert post(client, alert()).json() == {"ok": True, "duplicate": True}


class MissingEnvBreaks(FakeBroker):
    def missing_env(self):
        raise RuntimeError("config broke")


def test_an_unexpected_error_before_place_started_is_a_424_and_not_remembered():
    client, _, _ = fake_app(MissingEnvBreaks)
    first = post(client, alert())
    second = post(client, alert())
    assert (first.status_code, first.json()) == (424, {"ok": False, "error": UNEXPECTED})
    assert (second.status_code, second.json()) == (424, {"ok": False, "error": UNEXPECTED})


def test_a_lock_failure_before_the_order_is_a_424_and_not_remembered(monkeypatch):
    client, _, broker = fake_app()

    def broken_get(self, broker_name, symbol):
        raise RuntimeError("lock table broke")

    monkeypatch.setattr("tvbot.state.SymbolLocks.get", broken_get)
    first = post(client, alert())
    assert (first.status_code, first.json()) == (424, {"ok": False, "error": UNEXPECTED})
    assert post(client, alert()).status_code == 424
    assert broker.sent == 0


def test_broker_message_is_scrubbed_before_it_is_cut_to_300_characters():
    message = "x" * 295 + WEBHOOK_SECRET
    client, _, _ = fake_app(lambda s: FakeBroker(s, **_prepare_raises(BrokerRejected(400, message))))
    error = post(client, alert()).json()["error"]
    assert error == "x" * 295 + "<reda"


class Exploding(FakeBroker):
    """Raises a secret-carrying error from one chosen place."""

    reads_positions = True

    def __init__(self, settings, where):
        super().__init__(settings)
        self.where = where

    def _boom(self, where):
        if self.where == where:
            raise RuntimeError("boom " + " ".join(ALL_SECRETS))

    def validate(self, intent):
        self._boom("validate")

    def missing_env(self):
        self._boom("missing_env")
        return []

    def pre_check(self, intent):
        self._boom("pre_check")

    def held_quantity(self, symbol):
        self._boom("held_quantity")
        return Decimal(10)

    def preview(self, intent):
        self._boom("preview")
        return {}

    def prepare(self, intent, cid):
        self._boom("prepare")
        return {}

    def send(self, prepared, intent, cid):
        self._boom("send")
        return BrokerResult("x", 200, cid, "1")

    def place(self, intent, clamp_sells):
        self._boom("place")
        return super().place(intent, clamp_sells)


EXPLOSIONS = [
    ("validate", 424),
    ("missing_env", 424),
    ("pre_check", 424),
    ("held_quantity", 424),
    ("prepare", 424),
    ("send", 504),
    ("place", 504),
]


@pytest.mark.parametrize("where,expected", EXPLOSIONS)
def test_an_exploding_broker_never_yields_a_5xx_other_than_504(where, expected, caplog):
    caplog.set_level(logging.DEBUG)
    client, _, _ = fake_app(lambda s: Exploding(s, where))
    response = post(client, alert(action="sell"))
    assert response.status_code == expected
    assert response.status_code < 500 or response.status_code == 504
    assert response.json()["ok"] is False
    assert_no_secrets(caplog, response.text)


def test_an_exploding_preview_in_dry_run_is_a_424_with_nothing_leaked(caplog):
    caplog.set_level(logging.DEBUG)
    settings = make_settings()
    client, app = build(settings, Router())
    app.state.brokers["alpaca"] = Exploding(settings, "preview")
    response = post(client, alert())
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": UNEXPECTED})
    assert_no_secrets(caplog, response.text)
    assert "<redacted>" in caplog.text  # the traceback was logged, scrubbed


# the duplicate guard


def test_identical_body_inside_the_window_is_a_duplicate_with_one_broker_call():
    client, _, broker = fake_app()
    first = post(client, alert())
    second = post(client, alert())
    assert first.status_code == 200 and first.json()["order_id"] == "fake-1"
    assert (second.status_code, second.json()) == (200, {"ok": True, "duplicate": True})
    assert broker.sent == 1


def test_duplicate_is_logged_as_a_warning(caplog):
    caplog.set_level(logging.DEBUG)
    client, _, _ = fake_app()
    post(client, alert())
    post(client, alert())
    last = tvbot_records(caplog)[-1]
    assert last.levelno == logging.WARNING
    assert last.getMessage().endswith("status=200 order_id=- client_id=-: duplicate alert body skipped")


def test_identical_body_after_the_window_is_placed_again():
    clock = FakeClock()
    settings = make_settings(DRY_RUN="false", DEDUPE_SECONDS="30")
    client, app = build(settings, Router(), clock=clock)
    broker = FakeBroker(settings)
    app.state.brokers["alpaca"] = broker
    assert post(client, alert()).status_code == 200
    clock.advance(29)
    assert post(client, alert()).json() == {"ok": True, "duplicate": True}
    clock.advance(1)
    again = post(client, alert())
    assert again.status_code == 200 and again.json().get("duplicate") is None
    assert broker.sent == 2


def test_a_different_alert_id_is_placed():
    client, _, broker = fake_app()
    assert post(client, alert(alert_id="2026-11-20T10:00:00Z")).status_code == 200
    assert post(client, alert(alert_id="2026-11-20T10:00:01Z")).status_code == 200
    assert broker.sent == 2


def test_a_different_quantity_is_placed():
    client, _, broker = fake_app()
    post(client, alert(quantity=1))
    post(client, alert(quantity=2))
    assert broker.sent == 2


def test_after_a_424_the_same_body_is_placed_again():
    calls = {"n": 0}

    def hook(prepared, intent, cid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokerRejected(None, "could not reach alpaca: ConnectError")
        return BrokerResult("fake-2", 200, cid, "1")

    client, _, broker = fake_app(lambda s: FakeBroker(s, on_send=hook))
    assert post(client, alert()).status_code == 424
    second = post(client, alert())
    assert (second.status_code, second.json()["order_id"]) == (200, "fake-2")
    assert broker.sent == 2


def test_dedupe_seconds_zero_turns_the_guard_off():
    client, _, broker = fake_app(DEDUPE_SECONDS="0")
    assert post(client, alert()).status_code == 200
    assert post(client, alert()).status_code == 200
    assert broker.sent == 2


def test_identical_body_while_the_first_is_still_running_is_a_duplicate_even_after_the_window():
    clock = FakeClock()
    settings = make_settings(DRY_RUN="false", DEDUPE_SECONDS="30")
    client, app = build(settings, Router(), clock=clock)
    broker = GatedBroker(settings)
    app.state.brokers["alpaca"] = broker
    results: dict[str, httpx.Response] = {}

    def first():
        results["first"] = post(client, alert())

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert broker.entered.wait(timeout=10)
        assert post(client, alert()).json() == {"ok": True, "duplicate": True}
        clock.advance(300)  # ten windows later the first request is still running
        assert post(client, alert()).json() == {"ok": True, "duplicate": True}
    finally:
        broker.gate.set()
        thread.join(timeout=10)
    assert results["first"].status_code == 200
    assert broker.sent == 1


def test_a_failure_in_the_guard_bookkeeping_cannot_change_the_answer(monkeypatch):
    client, app, _ = fake_app()

    def broken(key, keep):
        raise RuntimeError("bookkeeping broke")

    monkeypatch.setattr(app.state.dedupe, "finish", broken)
    response = post(client, alert())
    assert (response.status_code, response.json()["order_id"]) == (200, "fake-1")


# the logger can never change an answer


class ExplodingHandler(logging.Handler):
    def emit(self, record):
        raise RuntimeError("log sink is down")


def test_a_logger_that_raises_after_a_successful_order_still_answers_200():
    client, _, broker = fake_app()
    logger = logging.getLogger("tvbot")
    handler = ExplodingHandler()
    logger.addHandler(handler)
    try:
        response = post(client, alert())
    finally:
        logger.removeHandler(handler)
    assert (response.status_code, response.json()["order_id"]) == (200, "fake-1")
    assert broker.sent == 1


def test_a_logger_that_raises_during_a_refusal_still_answers_422():
    client, _, _ = fake_app()
    logger = logging.getLogger("tvbot")
    handler = ExplodingHandler()
    logger.addHandler(handler)
    try:
        response = post(client, alert(symbol="!!"))
        wrong = post(client, alert(secret="wrong"))
    finally:
        logger.removeHandler(handler)
    assert (response.status_code, wrong.status_code) == (422, 401)


def test_a_scrubber_that_fails_still_lets_the_request_finish(caplog):
    caplog.set_level(logging.DEBUG)
    client, app, broker = fake_app(lambda s: FakeBroker(s, **_prepare_raises(BrokerRejected(400, f"bad {WEBHOOK_SECRET}"))))

    def broken(text):
        raise RuntimeError("scrubber broke")

    app.state.scrubber.scrub = broken
    refused = post(client, alert())
    assert refused.status_code == 424
    assert refused.json()["error"] == "details withheld: scrub failed"
    assert WEBHOOK_SECRET not in refused.text
    assert "log message dropped: scrub failed" in caplog.text
    assert WEBHOOK_SECRET not in all_log_text(caplog)

    client2, app2, broker2 = fake_app()
    app2.state.scrubber.scrub = broken
    ok = post(client2, alert())
    assert (ok.status_code, ok.json()["order_id"]) == (200, "fake-1")


def test_the_request_body_headers_and_secret_are_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    client, _, _ = fake_app()
    post(client, alert(note="unique-body-marker-7781"), headers={"X-Probe": "unique-header-marker-5512"})
    post(client, alert(secret="wrong-secret-marker-9914"))
    post(client, content=b"raw-garbage-marker-3306")
    text = all_log_text(caplog)
    for marker in ("unique-body-marker-7781", "unique-header-marker-5512", "wrong-secret-marker-9914", "raw-garbage-marker-3306", WEBHOOK_SECRET):
        assert marker not in text


def test_refusals_are_logged_as_warnings_with_their_status(caplog):
    caplog.set_level(logging.DEBUG)
    client, _, _ = fake_app()
    post(client, alert(secret="wrong"))
    post(client, content=b"[]")
    post(client, alert(symbol="!!"))
    warnings = [r.getMessage() for r in tvbot_records(caplog) if r.levelno == logging.WARNING]
    assert "webhook refused status=401: unauthorized" in warnings
    assert "webhook refused status=400: body must be a JSON object" in warnings
    assert "webhook refused status=422: invalid symbol" in warnings


def test_a_422_from_a_broker_guard_is_logged_with_the_alert_fields(caplog):
    caplog.set_level(logging.DEBUG)
    client, _, _ = fake_app(lambda s: FakeBroker(s, **_prepare_raises(GuardRefused("guard says no"))))
    post(client, alert())
    last = tvbot_records(caplog)[-1]
    assert last.levelno == logging.WARNING
    assert last.getMessage() == (
        "alert broker=alpaca symbol=AAPL action=buy qty=1 type=market tif=day dry_run=False "
        "status=422 order_id=- client_id=-: guard says no"
    )


# locks and the event loop


def test_same_symbol_orders_run_one_at_a_time_and_other_symbols_in_parallel():
    settings = make_settings(DRY_RUN="false", DEDUPE_SECONDS="0")
    client, app = build(settings, Router())
    broker = GatedBroker(settings)
    app.state.brokers["alpaca"] = broker
    threads = [threading.Thread(target=lambda q=q: post(client, alert(quantity=q))) for q in (1, 2)]
    other = threading.Thread(target=lambda: post(client, alert(symbol="MSFT")))
    for thread in threads:
        thread.start()
    try:
        assert broker.entered.wait(timeout=10)
        time.sleep(0.4)
        assert broker.sent == 1  # the second AAPL order waits on the symbol lock
        other.start()
        deadline = time.monotonic() + 10
        while broker.sent < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert broker.sent == 2  # MSFT is not held up by AAPL
        assert broker.active == 2
    finally:
        broker.gate.set()
        for thread in (*threads, other):
            if thread.ident is not None:
                thread.join(timeout=10)
    assert broker.sent == 3
    assert broker.max_active == 2


def test_the_event_loop_stays_free_while_an_order_is_in_flight():
    settings = make_settings(DRY_RUN="false", DEDUPE_SECONDS="0")
    client, app = build(settings, Router())
    broker = GatedBroker(settings)
    app.state.brokers["alpaca"] = broker
    with client:  # one shared event loop for every request
        thread = threading.Thread(target=lambda: post(client, alert()))
        thread.start()
        try:
            assert broker.entered.wait(timeout=10)
            started = time.monotonic()
            health = client.get("/health")
            assert health.status_code == 200
            assert time.monotonic() - started < 2
        finally:
            broker.gate.set()
            thread.join(timeout=10)


# real adapter, outcome classification


def orders(router):
    return router.sent("POST", "/v2/orders")


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_a_request_that_never_left_is_a_424_and_is_not_remembered(exc):
    router = Router().on("POST", "/v2/orders", raising(exc("nothing sent")), reply(200, json={"id": "o-1"}))
    client, _ = build_live(router, {"DEDUPE_SECONDS": "30"})
    first = post(client, alert())
    assert first.status_code == 424
    assert first.json() == {
        "ok": False,
        "error": f"could not reach alpaca: {exc.__name__}",
        "broker_status": None,
    }
    second = post(client, alert())
    assert (second.status_code, second.json()["order_id"]) == (200, "o-1")
    assert len(orders(router)) == 2


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteError]
)
def test_a_request_that_may_have_arrived_is_a_504_and_is_remembered(exc):
    router = Router().on("POST", "/v2/orders", raising(exc("who knows")))
    client, _ = build_live(router, {"DEDUPE_SECONDS": "30"})
    first = post(client, alert())
    sent_cid = json.loads(orders(router)[0].content)["client_order_id"]
    assert first.status_code == 504
    assert first.json() == {
        "ok": False,
        "error": f"outcome unknown ({exc.__name__}); the order may have been placed. "
        f"Check your broker for client id {sent_cid}",
    }
    assert post(client, alert()).json() == {"ok": True, "duplicate": True}
    assert len(orders(router)) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_broker_5xx_is_a_504_and_is_remembered(status):
    router = Router().on("POST", "/v2/orders", reply(status, json={"message": "upstream trouble"}))
    client, _ = build_live(router, {"DEDUPE_SECONDS": "30"})
    first = post(client, alert())
    assert first.status_code == 504
    assert first.json()["error"].startswith(f"outcome unknown (alpaca returned HTTP {status}); the order may have been placed.")
    assert post(client, alert()).json() == {"ok": True, "duplicate": True}
    assert len(orders(router)) == 1


def test_a_broker_4xx_is_a_424_with_its_status_and_is_not_remembered():
    router = Router().on("POST", "/v2/orders", reply(403, json={"message": "insufficient buying power"}))
    client, _ = build_live(router, {"DEDUPE_SECONDS": "30"})
    first = post(client, alert())
    assert (first.status_code, first.json()) == (
        424,
        {"ok": False, "error": "insufficient buying power", "broker_status": 403},
    )
    assert post(client, alert()).status_code == 424
    assert len(orders(router)) == 2


def test_a_read_timeout_on_the_position_read_is_a_424_and_no_order_is_sent():
    router = Router().on("GET", "/v2/positions", raising(httpx.ReadTimeout("slow")))
    client, _ = build_live(router, {"DEDUPE_SECONDS": "30"})
    first = post(client, alert(action="sell"))
    assert first.status_code == 424
    assert first.json() == {"ok": False, "error": "alpaca request failed: ReadTimeout", "broker_status": None}
    assert orders(router) == []
    assert post(client, alert(action="sell")).status_code == 424  # not remembered


def test_a_non_http_error_from_the_transport_is_a_504_for_an_order_and_a_424_for_a_read():
    router = Router().on("POST", "/v2/orders", raising(OSError("socket exploded")))
    client, _ = build_live(router)
    assert post(client, alert()).status_code == 504

    router = Router().on("GET", "/v2/positions", raising(OSError("socket exploded")))
    client, _ = build_live(router)
    response = post(client, alert(action="sell"))
    assert response.status_code == 424
    assert response.json()["error"] == "alpaca could not check the position: OSError"


# secrets never leave the process


RUNTIME_TOKEN = "runtime-access-token-0001"
RUNTIME_HASH = "RUNTIMEHASH0001"


def leaky_text(*values: str) -> str:
    return "rejected " + " | ".join(values)


def leak_case_alpaca():
    router = Router().on("POST", "/v2/orders", reply(403, json={"message": leaky_text(*ALL_SECRETS)}))
    return build_live(router)[0], [*ALL_SECRETS], "alpaca"


def leak_case_tradier():
    errors = {"errors": {"error": [f"Invalid account {TRADIER_ACCOUNT}", f"bad token {TRADIER_TOKEN}", WEBHOOK_SECRET]}}
    router = Router().on("POST", f"/v1/accounts/{TRADIER_ACCOUNT}/orders", reply(200, json=errors))
    return build_live(router)[0], [*ALL_SECRETS], "tradier"


def leak_case_tastytrade():
    router = Router()
    router.on("POST", "/oauth/token", reply(200, json={"access_token": RUNTIME_TOKEN, "expires_in": 900}))
    message = leaky_text(RUNTIME_TOKEN, TASTY_ACCOUNT, TASTY_CLIENT_SECRET, WEBHOOK_SECRET)
    router.on("POST", f"/accounts/{TASTY_ACCOUNT}/orders", reply(400, json={"error": {"code": "bad", "message": message}}))
    return build_live(router)[0], [*ALL_SECRETS, RUNTIME_TOKEN], "tastytrade"


def leak_case_schwab():
    def refuse(account_hash, builder):
        return httpx.Response(400, text=leaky_text(account_hash, SCHWAB_ACCOUNT, WEBHOOK_SECRET))

    def accounts():
        return httpx.Response(200, json=[{"accountNumber": SCHWAB_ACCOUNT, "hashValue": RUNTIME_HASH}])

    fake = FakeSchwabClient(on_accounts=accounts, on_place=refuse)
    client, _ = build_live(Router(), {"SCHWAB_LIVE": "true"}, schwab_client_factory=CountingFactory(fake))
    return client, [*ALL_SECRETS, RUNTIME_HASH], "schwab"


def leak_case_alpaca_exception():
    text = f"timeout talking to https://paper-api.alpaca.markets/v2/orders as {ALL_SECRETS[1]} {ALL_SECRETS[2]}"
    router = Router().on("POST", "/v2/orders", raising(httpx.ReadTimeout(text)))
    return build_live(router)[0], [*ALL_SECRETS], "alpaca"


def leak_case_tradier_exception():
    text = f"timeout talking to https://sandbox.tradier.com/v1/accounts/{TRADIER_ACCOUNT}/orders with {TRADIER_TOKEN}"
    router = Router().on("POST", f"/v1/accounts/{TRADIER_ACCOUNT}/orders", raising(httpx.ReadTimeout(text)))
    return build_live(router)[0], [*ALL_SECRETS], "tradier"


def leak_case_tastytrade_exception():
    router = Router()
    router.on("POST", "/oauth/token", reply(200, json={"access_token": RUNTIME_TOKEN, "expires_in": 900}))
    text = f"timeout for account {TASTY_ACCOUNT} with {RUNTIME_TOKEN}"
    router.on("POST", f"/accounts/{TASTY_ACCOUNT}/orders", raising(httpx.ReadTimeout(text)))
    return build_live(router)[0], [*ALL_SECRETS, RUNTIME_TOKEN], "tastytrade"


def leak_case_schwab_exception():
    def explode(account_hash, builder):
        raise RuntimeError(f"failed for {account_hash} {SCHWAB_ACCOUNT}")

    def accounts():
        return httpx.Response(200, json=[{"accountNumber": SCHWAB_ACCOUNT, "hashValue": RUNTIME_HASH}])

    fake = FakeSchwabClient(on_accounts=accounts, on_place=explode)
    client, _ = build_live(Router(), {"SCHWAB_LIVE": "true"}, schwab_client_factory=CountingFactory(fake))
    return client, [*ALL_SECRETS, RUNTIME_HASH], "schwab"


LEAK_CASES = [
    leak_case_alpaca,
    leak_case_tradier,
    leak_case_tastytrade,
    leak_case_schwab,
    leak_case_alpaca_exception,
    leak_case_tradier_exception,
    leak_case_tastytrade_exception,
    leak_case_schwab_exception,
]


@pytest.mark.parametrize("case", LEAK_CASES, ids=lambda c: c.__name__.removeprefix("leak_case_"))
def test_a_failing_order_leaks_no_secret_into_the_response_or_the_logs(case, caplog):
    caplog.set_level(logging.DEBUG)
    client, secrets, broker = case()
    response = post(client, alert(broker=broker))
    assert response.status_code in (424, 504)
    for secret in secrets:
        assert secret not in response.text
    assert_no_secrets(caplog, response.text, secrets=secrets)
    assert not [r for r in caplog.records if r.name.startswith(("httpx", "httpcore"))]


def test_a_secret_in_a_position_read_error_does_not_leak(caplog):
    caplog.set_level(logging.DEBUG)
    router = Router().on("GET", "/v2/positions", reply(401, json={"message": leaky_text(*ALL_SECRETS)}))
    client, _ = build_live(router)
    response = post(client, alert(action="sell"))
    assert response.status_code == 424
    assert_no_secrets(caplog, response.text)


def test_an_exception_through_the_catch_all_leaks_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    client, _, _ = fake_app(lambda s: Exploding(s, "validate"))
    response = post(client, alert())
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": UNEXPECTED})
    assert_no_secrets(caplog, response.text)
    assert "<redacted>" in caplog.text


def test_every_response_body_is_json_with_an_ok_field_and_no_secret():
    client, _, _ = fake_app()
    responses = [
        post(client, alert()),
        post(client, alert(secret="wrong")),
        post(client, content=b"[]"),
        post(client, alert(symbol="!!")),
        client.get("/health"),
        client.get("/nope"),
        client.get("/webhook"),
        post(client, content=b" " * 20000),
    ]
    for response in responses:
        assert response.headers["content-type"].startswith("application/json")
        body = response.json()
        assert isinstance(body["ok"], bool)
        assert WEBHOOK_SECRET not in response.text


# the catch-all middleware on its own


def run_middleware(inner, scope=None, send=None):
    sent: list[dict] = []

    async def default_send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.disconnect"}

    scope = scope or {"type": "http", "path": "/x", "method": "GET", "headers": []}
    asyncio.run(CatchAllMiddleware(inner)(scope, receive, send or default_send))
    return sent


def test_catch_all_answers_424_for_an_exception_before_any_response():
    async def inner(scope, receive, send):
        raise RuntimeError("boom")

    sent = run_middleware(inner)
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 424
    assert json.loads(sent[1]["body"]) == {"ok": False, "error": UNEXPECTED}


def test_catch_all_never_reraises_and_adds_nothing_after_a_response_started():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("late failure")

    sent = run_middleware(inner)
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert sent[0]["status"] == 200


def test_catch_all_survives_a_send_that_fails_too():
    async def inner(scope, receive, send):
        raise RuntimeError("boom")

    async def broken_send(message):
        raise OSError("client went away")

    assert run_middleware(inner, send=broken_send) == []


def test_catch_all_passes_other_scope_types_straight_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    run_middleware(inner, scope={"type": "lifespan"})
    assert seen == ["lifespan"]


# create_app itself


def test_create_app_reads_the_environment_when_given_no_settings(monkeypatch):
    for name in list(make_env()):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError, match="WEBHOOK_SECRET must be set to at least 16 characters"):
        create_app()
    monkeypatch.setenv("WEBHOOK_SECRET", "e" * 32)
    app = create_app(schwab_client_factory=CountingFactory())
    client = TestClient(app, raise_server_exceptions=False)
    assert post(client, alert(secret="e" * 32)).status_code == 200


def test_the_owned_http_client_uses_the_broker_timeout_and_is_closed_on_shutdown(monkeypatch):
    created = []

    class RecordingClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr("tvbot.app.httpx.Client", RecordingClient)
    app = create_app(make_settings(BROKER_TIMEOUT_SECONDS="2.5"), schwab_client_factory=CountingFactory())
    assert [c.kwargs for c in created] == [{"timeout": 2.5}]
    with TestClient(app):
        assert created[0].closed is False
    assert created[0].closed is True


def test_an_injected_http_client_is_left_open_on_shutdown():
    router = Router()
    injected = router.client()
    app = create_app(make_settings(), http_client=injected, schwab_client_factory=CountingFactory())
    with TestClient(app):
        pass
    assert injected.is_closed is False
