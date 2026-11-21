"""tastytrade adapter: OAuth token handling, request shape, position reads, the sell guard and error text."""

from __future__ import annotations

import logging
import re
import threading
from decimal import Decimal

import httpx
import pytest
from conftest import (
    ALL_SECRETS,
    TASTY_ACCOUNT,
    TASTY_CLIENT_SECRET,
    TASTY_REFRESH,
    FakeClock,
    Router,
    alert,
    assert_no_secrets,
    body_json,
    build,
    build_live,
    make_settings,
    raising,
    reply,
)

from tvbot.brokers.base import BrokerRejected, OrderIntent
from tvbot.brokers.tastytrade import TastytradeBroker
from tvbot.logs import Scrubber

TOKEN = "/oauth/token"
ORDERS = f"/accounts/{TASTY_ACCOUNT}/orders"
POSITIONS = f"/accounts/{TASTY_ACCOUNT}/positions"
SANDBOX = "https://api.cert.tastyworks.com"
LIVE = "https://api.tastyworks.com"
USER_AGENT = "tradingview-webhook-trading-bot/0.1.0"
CID = re.compile(r"tvbot-[0-9a-f]{24}")
NO_LONG = "no long position in AAPL to sell; this bot does not open short positions"
MISSING = object()
INTENT = OrderIntent("tastytrade", "AAPL", "buy", Decimal(1), "market", None, "day")


def post(client, payload):
    return client.post("/webhook", json=payload)


def tasty(**extra):
    return alert(broker="tastytrade", **extra)


def token_reply(token="tasty-access-1", **fields):
    body = {"access_token": token, "token_type": "Bearer", "expires_in": 900}
    body.update(fields)
    return reply(200, json={key: value for key, value in body.items() if value is not MISSING})


def order_ok(order_id="771043", status=201):
    body = {
        "data": {"order": {"id": order_id, "status": "Routed"}, "buying-power-effect": {}},
        "context": f"/accounts/{TASTY_ACCOUNT}/orders",
    }
    return reply(status, json=body)


def envelope(code, message, *details):
    error = {"code": code, "message": message}
    if details:
        error["errors"] = [{"code": code, "message": detail} for detail in details]
    return {"error": error}


def holding(symbol="AAPL", quantity="100", direction="Long", kind="Equity"):
    return {
        "account-number": TASTY_ACCOUNT,
        "symbol": symbol,
        "instrument-type": kind,
        "quantity": quantity,
        "quantity-direction": direction,
        "average-open-price": "288.7",
    }


def listing(*items):
    return {"data": {"items": list(items)}, "context": f"/accounts/{TASTY_ACCOUNT}/positions"}


def tasty_router(order_reply=None, positions=None, tokens=None):
    router = Router().on("POST", TOKEN, *(tokens or [token_reply()]))
    router.on("POST", ORDERS, order_reply or order_ok())
    if positions is not None:
        router.on("GET", POSITIONS, reply(200, json=positions))
    return router


def sent_order(router):
    [request] = router.sent("POST", ORDERS)
    return request


def token_requests(router):
    return router.sent("POST", TOKEN)


def direct(router, clock=None, **env):
    settings = make_settings(**env)
    scrubber = Scrubber(settings)
    broker = TastytradeBroker(settings, router.client(), scrubber=scrubber, clock=clock or FakeClock())
    return broker, scrubber


# request shape


def test_token_is_fetched_first_and_the_market_buy_matches_the_api_reference():
    router = tasty_router()
    client, _ = build_live(router)
    response = post(client, tasty())
    assert response.status_code == 200
    assert [(r.method, r.url.path) for r in router.requests] == [("POST", TOKEN), ("POST", ORDERS)]

    [token_request] = token_requests(router)
    assert str(token_request.url) == f"{SANDBOX}/oauth/token"
    assert body_json(token_request) == {
        "grant_type": "refresh_token",
        "refresh_token": TASTY_REFRESH,
        "client_secret": TASTY_CLIENT_SECRET,
    }
    assert token_request.headers["User-Agent"] == USER_AGENT
    assert token_request.headers["Accept"] == "application/json"
    assert token_request.headers["Content-Type"] == "application/json"
    assert "Authorization" not in token_request.headers

    request = sent_order(router)
    body = body_json(request)
    assert str(request.url) == f"{SANDBOX}/accounts/{TASTY_ACCOUNT}/orders"
    assert request.headers["Authorization"] == "Bearer tasty-access-1"
    assert request.headers["User-Agent"] == USER_AGENT
    assert request.headers["Accept"] == "application/json"
    assert CID.fullmatch(body["external-identifier"])
    assert body == {
        "time-in-force": "Day",
        "order-type": "Market",
        "source": "tradingview-webhook-trading-bot",
        "external-identifier": body["external-identifier"],
        "legs": [{"instrument-type": "Equity", "symbol": "AAPL", "quantity": "1", "action": "Buy to Open"}],
    }
    assert response.json() == {
        "ok": True,
        "dry_run": False,
        "broker": "tastytrade",
        "order_id": "771043",
        "client_id": body["external-identifier"],
        "quantity": "1",
    }


@pytest.mark.parametrize(
    "extra,effect,tif,action",
    [
        ({"action": "buy"}, "Debit", "Day", "Buy to Open"),
        ({"action": "buy", "time_in_force": "gtc"}, "Debit", "GTC", "Buy to Open"),
        ({"action": "sell"}, "Credit", "Day", "Sell to Close"),
        ({"action": "sell", "time_in_force": "gtc"}, "Credit", "GTC", "Sell to Close"),
    ],
)
def test_limit_orders_carry_a_string_price_and_the_matching_price_effect(extra, effect, tif, action):
    router = tasty_router(positions=listing(holding("AAPL", "50")))
    client, _ = build_live(router)
    response = post(client, tasty(quantity=3, order_type="limit", limit_price="187.50", **extra))
    assert response.status_code == 200
    body = body_json(sent_order(router))
    assert (body["order-type"], body["time-in-force"]) == ("Limit", tif)
    assert (body["price"], body["price-effect"]) == ("187.50", effect)
    assert body["legs"] == [{"instrument-type": "Equity", "symbol": "AAPL", "quantity": "3", "action": action}]


def test_a_market_order_has_no_price_even_when_the_alert_names_one():
    router = tasty_router()
    client, _ = build_live(router)
    post(client, tasty(limit_price=99))
    body = body_json(sent_order(router))
    assert "price" not in body and "price-effect" not in body


@pytest.mark.parametrize("quantity,expected", [(2.0, "2"), ("3", "3"), (100, "100"), ("4.000", "4")])
def test_whole_quantities_are_sent_as_integer_strings(quantity, expected):
    router = tasty_router()
    client, _ = build_live(router)
    assert post(client, tasty(quantity=quantity)).status_code == 200
    assert body_json(sent_order(router))["legs"][0]["quantity"] == expected


@pytest.mark.parametrize("quantity", [0.5, "2.25"])
def test_fractional_quantity_is_refused_before_any_request(quantity):
    router = tasty_router()
    client, _ = build_live(router)
    response = post(client, tasty(quantity=quantity))
    assert (response.status_code, response.json()) == (
        422,
        {"ok": False, "error": "tastytrade orders must be whole shares in this bot"},
    )
    assert router.requests == []


@pytest.mark.parametrize("dry", [False, True])
def test_a_gtc_market_order_is_refused_before_any_request(dry):
    router = tasty_router()
    client, _ = build(make_settings(DRY_RUN="true" if dry else "false"), router)
    response = post(client, tasty(time_in_force="gtc"))
    assert (response.status_code, response.json()) == (
        422,
        {"ok": False, "error": "tastytrade market orders must be day orders"},
    )
    assert router.requests == []


def test_a_gtc_limit_order_is_allowed():
    router = tasty_router()
    client, _ = build_live(router)
    assert post(client, tasty(order_type="limit", limit_price=10, time_in_force="gtc")).status_code == 200


def test_sandbox_is_the_default_and_live_needs_the_flag():
    sandbox_router = tasty_router(positions=listing(holding()))
    sandbox, _ = build_live(sandbox_router)
    post(sandbox, tasty(action="sell"))
    assert {str(r.url).split("/accounts")[0].split("/oauth")[0] for r in sandbox_router.requests} == {SANDBOX}

    live_router = tasty_router(positions=listing(holding()))
    live, _ = build_live(live_router, {"TASTYTRADE_LIVE": "true"})
    post(live, tasty(action="sell"))
    assert {str(r.url).split("/accounts")[0].split("/oauth")[0] for r in live_router.requests} == {LIVE}
    assert str(token_requests(live_router)[0].url) == f"{LIVE}/oauth/token"
    assert str(sent_order(live_router).url) == f"{LIVE}/accounts/{TASTY_ACCOUNT}/orders"
    assert str(live_router.sent("GET", POSITIONS)[0].url).startswith(f"{LIVE}/accounts/{TASTY_ACCOUNT}/positions")


def test_the_account_number_is_percent_encoded_in_the_path():
    settings = make_settings(DRY_RUN="false", DEDUPE_SECONDS="0", TASTYTRADE_ACCOUNT_NUMBER="5WX 9/9?x")
    router = Router().on("POST", TOKEN, token_reply()).on("POST", "/accounts/5WX 9/9?x/orders", order_ok())
    client, _ = build(settings, router)
    assert post(client, tasty()).status_code == 200
    assert router.sent("POST", "/accounts/5WX 9/9?x/orders")[0].url.raw_path == b"/accounts/5WX%209%2F9%3Fx/orders"


def test_dry_run_preview_uses_a_placeholder_never_fetches_a_token_and_the_flag_picks_the_host():
    router = Router()
    sandbox, _ = build(router=router)
    live, _ = build(make_settings(TASTYTRADE_LIVE="true"), router)
    one = post(sandbox, tasty()).json()
    two = post(live, tasty()).json()
    assert one["dry_run"] is True
    assert one["request"]["url"] == f"{SANDBOX}/accounts/<TASTYTRADE_ACCOUNT_NUMBER>/orders"
    assert two["request"]["url"] == f"{LIVE}/accounts/<TASTYTRADE_ACCOUNT_NUMBER>/orders"
    assert TASTY_ACCOUNT not in one["request"]["url"]
    assert router.requests == []  # no token call and no other call in a dry run
    assert post(sandbox, tasty(action="sell")).status_code == 200  # nor does a sell read a position
    assert router.requests == []


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"quantity": 7},
        {"order_type": "limit", "limit_price": 10.5, "time_in_force": "gtc", "quantity": 2},
        {"action": "sell", "quantity": 4},
        {"action": "sell", "order_type": "limit", "limit_price": "99.95", "quantity": 1},
    ],
)
def test_the_preview_equals_the_request_that_is_sent(extra):
    payload = tasty(**extra)
    preview = post(build()[0], payload).json()["request"]

    router = tasty_router(positions=listing(holding("AAPL", "10")))
    client, _ = build_live(router)
    assert post(client, payload).status_code == 200
    request = sent_order(router)

    sent = body_json(request)
    assert CID.fullmatch(preview["json"]["external-identifier"]) and CID.fullmatch(sent["external-identifier"])
    assert preview["json"]["external-identifier"] != sent["external-identifier"]
    assert (preview["method"], preview["url"]) == (request.method, str(request.url).replace(TASTY_ACCOUNT, "<TASTYTRADE_ACCOUNT_NUMBER>"))
    assert {k: v for k, v in preview["json"].items() if k != "external-identifier"} == {
        k: v for k, v in sent.items() if k != "external-identifier"
    }


# responses


@pytest.mark.parametrize(
    "answer,expected",
    [
        (order_ok("771043"), "771043"),
        (order_ok("771043", status=200), "771043"),
        (order_ok(771043), "771043"),
        (reply(201, json={"data": {"order": {"status": "Routed"}}}), None),
        (reply(201, json={"data": {"order": {"id": None}}}), None),
        (reply(201, json={"data": {"order": "x"}}), None),
        (reply(201, json={"data": []}), None),
        (reply(201, json={}), None),
        (reply(201, json=["x"]), None),
        (reply(201, text="created"), None),
        (reply(204), None),
    ],
)
def test_a_2xx_answer_is_a_success_whatever_it_carries(answer, expected):
    client, _ = build_live(tasty_router(answer))
    response = post(client, tasty())
    assert response.status_code == 200
    assert response.json()["order_id"] == expected


@pytest.mark.parametrize(
    "body,message",
    [
        (
            envelope("preflight_check_failure", "One or more preflight checks failed", "A specific validation failed for this order."),
            "One or more preflight checks failed; A specific validation failed for this order.",
        ),
        (envelope("preflight_check_failure", "One or more preflight checks failed"), "One or more preflight checks failed"),
        ({"error": {"code": "x", "message": "m", "errors": []}}, "m"),
        ({"error": {"code": "x", "message": "m", "errors": ["junk"]}}, "m"),
        ({"error": {"code": "x", "message": "m", "errors": [{"message": 5}]}}, "m"),
        ({"error": {"code": "x", "message": "m", "errors": [{"message": ""}]}}, "m"),
        ({"error": {"code": "x", "errors": [{"message": "only the detail"}]}}, "only the detail"),
        ({"error": {"code": "x", "message": 5}}, '{"error":{"code":"x","message":5}}'),
        ({"error": {}}, '{"error":{}}'),
        ({"error": "text"}, '{"error":"text"}'),
        ({"other": 1}, '{"other":1}'),
    ],
)
def test_the_error_envelope_message_is_extracted(body, message):
    client, _ = build_live(tasty_router(reply(422, json=body)))
    response = post(client, tasty())
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": message, "broker_status": 422})


@pytest.mark.parametrize("status", [400, 403, 404, 406, 422, 429])
def test_any_4xx_is_a_424_that_carries_the_status(status):
    client, _ = build_live(tasty_router(reply(status, json=envelope("c", "no"))))
    response = post(client, tasty())
    assert (response.status_code, response.json()["broker_status"]) == (424, status)


def test_a_non_json_error_body_is_passed_through_as_text():
    client, _ = build_live(tasty_router(reply(400, text="User-Agent header missing")))
    assert post(client, tasty()).json()["error"] == "User-Agent header missing"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_5xx_on_the_order_is_a_504_naming_the_id_and_is_never_resent(status):
    router = tasty_router(reply(status, text="upstream"))
    client, _ = build_live(router, {"DEDUPE_SECONDS": "30"})
    response = post(client, tasty())
    cid = body_json(sent_order(router))["external-identifier"]
    assert response.status_code == 504
    assert response.json() == {
        "ok": False,
        "error": f"outcome unknown (tastytrade returned HTTP {status}); the order may have been placed. Check your broker for client id {cid}",
    }
    assert len(router.sent("POST", ORDERS)) == 1
    assert post(client, tasty()).json() == {"ok": True, "duplicate": True}
    assert len(router.sent("POST", ORDERS)) == 1


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("slow"), httpx.WriteTimeout("slow"), httpx.ReadError("cut"), httpx.RemoteProtocolError("bad")]
)
def test_a_transport_failure_after_the_order_may_have_left_is_a_504(exc):
    router = Router().on("POST", TOKEN, token_reply()).on("POST", ORDERS, raising(exc))
    client, _ = build_live(router)
    response = post(client, tasty())
    cid = body_json(sent_order(router))["external-identifier"]
    assert response.status_code == 504
    assert response.json() == {
        "ok": False,
        "error": f"outcome unknown ({type(exc).__name__}); the order may have been placed. Check your broker for client id {cid}",
    }


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("busy")])
def test_a_failure_before_the_order_left_is_a_424(exc):
    router = Router().on("POST", TOKEN, token_reply()).on("POST", ORDERS, raising(exc))
    client, _ = build_live(router)
    response = post(client, tasty())
    assert (response.status_code, response.json()) == (
        424,
        {"ok": False, "error": f"could not reach tastytrade: {type(exc).__name__}", "broker_status": None},
    )


# the token


def test_one_token_serves_several_orders():
    router = tasty_router(positions=listing(holding("AAPL", "50")))
    client, _ = build_live(router)
    assert post(client, tasty()).status_code == 200
    assert post(client, tasty(quantity=2)).status_code == 200
    assert post(client, tasty(action="sell", quantity=3)).status_code == 200
    assert len(token_requests(router)) == 1
    assert {r.headers["Authorization"] for r in router.requests if r.url.path != TOKEN} == {"Bearer tasty-access-1"}
    assert len(router.sent("POST", ORDERS)) == 3


def test_every_call_identifies_itself_and_only_api_calls_carry_the_token():
    router = tasty_router(positions=listing(holding()))
    client, _ = build_live(router)
    post(client, tasty(action="sell"))
    assert len(router.requests) == 3
    for request in router.requests:
        assert request.headers["User-Agent"] == USER_AGENT
        assert request.headers["Accept"] == "application/json"
        if request.url.path == TOKEN:
            assert "Authorization" not in request.headers
        else:
            assert request.headers["Authorization"] == "Bearer tasty-access-1"


def test_a_401_on_the_order_refreshes_the_token_once_and_retries_the_same_order():
    router = tasty_router(
        order_reply=None,
        tokens=[token_reply("tasty-access-1"), token_reply("tasty-access-2")],
    )
    router.on("POST", ORDERS, reply(401, json=envelope("token_expired", "Token expired")), order_ok())
    client, _ = build_live(router)
    response = post(client, tasty())
    assert (response.status_code, response.json()["order_id"]) == (200, "771043")
    first, second = router.sent("POST", ORDERS)
    assert first.headers["Authorization"] == "Bearer tasty-access-1"
    assert second.headers["Authorization"] == "Bearer tasty-access-2"
    assert first.content == second.content  # the same order, the same external identifier
    assert len(token_requests(router)) == 2

    assert post(client, tasty()).status_code == 200  # the refreshed token stays cached
    assert len(token_requests(router)) == 2
    assert router.sent("POST", ORDERS)[2].headers["Authorization"] == "Bearer tasty-access-2"


def test_a_second_401_on_the_order_is_a_424_and_nothing_is_retried_again():
    router = tasty_router(tokens=[token_reply("tasty-access-1"), token_reply("tasty-access-2")])
    router.on("POST", ORDERS, reply(401, json=envelope("token_expired", "Token expired")))
    client, _ = build_live(router)
    response = post(client, tasty())
    assert (response.status_code, response.json()) == (
        424,
        {"ok": False, "error": "Token expired", "broker_status": 401},
    )
    assert len(router.sent("POST", ORDERS)) == 2
    assert len(token_requests(router)) == 2


def test_a_401_on_the_position_read_refreshes_the_token_and_retries():
    router = tasty_router(tokens=[token_reply("tasty-access-1"), token_reply("tasty-access-2")])
    router.on(
        "GET",
        POSITIONS,
        reply(401, json=envelope("token_expired", "Token expired")),
        reply(200, json=listing(holding("AAPL", "10"))),
    )
    client, _ = build_live(router)
    response = post(client, tasty(action="sell", quantity=4))
    assert response.status_code == 200
    first, second = router.sent("GET", POSITIONS)
    assert (first.headers["Authorization"], second.headers["Authorization"]) == ("Bearer tasty-access-1", "Bearer tasty-access-2")
    assert sent_order(router).headers["Authorization"] == "Bearer tasty-access-2"
    assert len(token_requests(router)) == 2


def test_a_second_401_on_the_position_read_is_a_424_and_no_order_is_sent():
    router = tasty_router(tokens=[token_reply("tasty-access-1"), token_reply("tasty-access-2")])
    router.on("GET", POSITIONS, reply(401, json=envelope("token_expired", "Token expired")))
    client, _ = build_live(router)
    response = post(client, tasty(action="sell"))
    assert (response.status_code, response.json()) == (
        424,
        {"ok": False, "error": "Token expired", "broker_status": 401},
    )
    assert len(router.sent("GET", POSITIONS)) == 2
    assert router.sent("POST", ORDERS) == []


@pytest.mark.parametrize(
    "answer,status,message",
    [
        (reply(400, json={"error": "invalid_grant", "error_description": "grant revoked"}), 400, '{"error":"invalid_grant","error_description":"grant revoked"}'),
        (reply(401, json=envelope("invalid_token", "Refresh token invalid")), 401, "Refresh token invalid"),
        (reply(403, text="forbidden"), 403, "forbidden"),
        (reply(500, text="down"), 500, "down"),
    ],
)
def test_a_failed_token_call_is_a_424_before_any_order_request(answer, status, message):
    router = Router().on("POST", TOKEN, answer).on("POST", ORDERS, order_ok())
    client, _ = build_live(router, {"DEDUPE_SECONDS": "30"})
    response = post(client, tasty())
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": message, "broker_status": status})
    assert router.sent("POST", ORDERS) == []
    router.on("POST", TOKEN, token_reply())
    assert post(client, tasty()).status_code == 200  # a refused attempt can be retried at once


@pytest.mark.parametrize(
    "answer",
    [
        reply(200, json={}),
        reply(200, json={"access_token": ""}),
        reply(200, json={"access_token": None}),
        reply(200, json={"access_token": 12345}),
        reply(200, json=["tasty-access-1"]),
        reply(200, text="not json"),
        reply(204),
    ],
)
def test_a_token_answer_without_an_access_token_is_a_424(answer):
    router = Router().on("POST", TOKEN, answer).on("POST", ORDERS, order_ok())
    client, _ = build_live(router)
    response = post(client, tasty())
    assert (response.status_code, response.json()["error"]) == (424, "tastytrade token response had no access_token")
    assert router.sent("POST", ORDERS) == []


@pytest.mark.parametrize(
    "exc,message",
    [
        (httpx.ConnectError("refused"), "could not reach tastytrade: ConnectError"),
        (httpx.ConnectTimeout("slow"), "could not reach tastytrade: ConnectTimeout"),
        (httpx.ReadTimeout("slow"), "tastytrade request failed: ReadTimeout"),
        (httpx.RemoteProtocolError("bad"), "tastytrade request failed: RemoteProtocolError"),
    ],
)
def test_a_transport_failure_on_the_token_call_is_a_424_and_no_order_is_sent(exc, message):
    router = Router().on("POST", TOKEN, raising(exc)).on("POST", ORDERS, order_ok())
    client, _ = build_live(router)
    response = post(client, tasty())
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": message, "broker_status": None})
    assert router.sent("POST", ORDERS) == []


def test_a_token_that_expires_between_orders_is_replaced():
    router = Router().on("POST", TOKEN, token_reply("tasty-access-1"), token_reply("tasty-access-2"))
    clock = FakeClock(1000.0)
    broker, _ = direct(router, clock)
    router.on("GET", POSITIONS, reply(200, json=listing()))

    broker.held_quantity("AAPL")  # fetched at t=1000, reusable until 1000 + 900 - 60
    clock.advance(839.0)
    broker.held_quantity("AAPL")
    assert len(token_requests(router)) == 1
    clock.advance(1.0)
    broker.held_quantity("AAPL")
    assert len(token_requests(router)) == 2
    assert [r.headers["Authorization"] for r in router.sent("GET", POSITIONS)] == [
        "Bearer tasty-access-1",
        "Bearer tasty-access-1",
        "Bearer tasty-access-2",
    ]


@pytest.mark.parametrize(
    "expires_in,usable_for",
    [(900, 840), (MISSING, 840), (None, 840), ("abc", 840), ([], 840), ("600", 540), (120, 60), (3600, 3540), (900.0, 840)],
)
def test_the_token_lifetime_comes_from_expires_in_with_a_minute_of_margin(expires_in, usable_for):
    router = Router().on("POST", TOKEN, token_reply("tasty-access-1", expires_in=expires_in))
    router.on("GET", POSITIONS, reply(200, json=listing()))
    clock = FakeClock(5000.0)
    broker, _ = direct(router, clock)
    broker.held_quantity("AAPL")
    clock.advance(usable_for - 0.5)
    broker.held_quantity("AAPL")
    assert len(token_requests(router)) == 1
    clock.advance(0.5)
    broker.held_quantity("AAPL")
    assert len(token_requests(router)) == 2


def test_a_token_with_under_a_minute_left_is_fetched_again_for_every_call():
    router = Router().on("POST", TOKEN, token_reply("tasty-access-1", expires_in=30))
    router.on("GET", POSITIONS, reply(200, json=listing()))
    broker, _ = direct(router)
    for _ in range(3):
        broker.held_quantity("AAPL")
    assert len(token_requests(router)) == 3


def test_concurrent_callers_share_one_token_fetch():
    router = Router().on("POST", TOKEN, token_reply("tasty-access-1"))
    broker, _ = direct(router)
    barrier = threading.Barrier(8)
    finished = []

    def worker():
        barrier.wait(timeout=10)
        finished.append(broker.prepare(INTENT, "tvbot-0123456789abcdef01234567"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(finished) == 8
    assert len(token_requests(router)) == 1


def test_a_caller_whose_token_was_already_replaced_does_not_fetch_another():
    router = Router().on("POST", TOKEN, token_reply("tasty-access-1"), token_reply("tasty-access-2"))
    # Replies are consumed in the order the router sees the requests: the nested read first, then the outer one.
    router.on(
        "GET",
        POSITIONS,
        reply(401, json=envelope("token_expired", "Token expired")),
        reply(200, json=listing(holding("MSFT", "7"))),
        reply(401, json=envelope("token_expired", "Token expired")),
        reply(200, json=listing(holding("AAPL", "5"))),
    )
    nested = {}

    def handler(request):
        if request.url.path == POSITIONS and "done" not in nested:
            nested["done"] = True
            nested["value"] = broker.held_quantity("MSFT")  # another caller refreshes the token meanwhile
        return router(request)

    settings = make_settings()
    broker = TastytradeBroker(settings, httpx.Client(transport=httpx.MockTransport(handler), timeout=5))
    assert broker.held_quantity("AAPL") == Decimal(5)
    assert nested["value"] == Decimal(7)
    assert len(token_requests(router)) == 2  # the outer caller reused the replacement
    assert [r.headers["Authorization"] for r in router.sent("GET", POSITIONS)] == [
        "Bearer tasty-access-1",
        "Bearer tasty-access-2",
        "Bearer tasty-access-1",
        "Bearer tasty-access-2",
    ]


def test_the_runtime_token_is_registered_with_the_scrubber():
    router = Router().on("POST", TOKEN, token_reply("tasty-access-1"))
    broker, scrubber = direct(router)
    assert scrubber.scrub("header tasty-access-1 end") == "header tasty-access-1 end"
    broker.prepare(INTENT, "tvbot-0123456789abcdef01234567")
    assert scrubber.scrub("header tasty-access-1 end") == "header <redacted> end"


def test_a_token_echoed_in_an_error_body_never_reaches_the_response_or_the_logs(caplog):
    caplog.set_level(logging.DEBUG)
    body = envelope("bad_request", "bearer tasty-access-1 was rejected", "also tasty-access-1")
    client, _ = build_live(tasty_router(reply(422, json=body)))
    response = post(client, tasty())
    assert response.status_code == 424
    assert "tasty-access-1" not in response.text and "<redacted>" in response.text
    assert_no_secrets(caplog, response.text, secrets=[*ALL_SECRETS, "tasty-access-1"])


def test_a_refresh_after_a_401_registers_the_new_token_too():
    router = tasty_router(tokens=[token_reply("tasty-access-1"), token_reply("tasty-access-2")])
    router.on("POST", ORDERS, reply(401, json=envelope("token_expired", "Token expired")), reply(422, json=envelope("x", "tasty-access-2 refused")))
    client, _ = build_live(router)
    response = post(client, tasty())
    assert (response.status_code, response.json()["error"]) == (424, "<redacted> refused")


# sells


def test_the_position_read_asks_for_the_symbol_and_sends_the_token():
    router = tasty_router(positions=listing(holding()))
    client, _ = build_live(router)
    post(client, tasty(action="sell", symbol="AAPL"))
    [request] = router.sent("GET", POSITIONS)
    assert request.url.params["symbol"] == "AAPL"
    assert request.url.query == b"symbol=AAPL"
    assert request.headers["Authorization"] == "Bearer tasty-access-1"
    assert request.content == b""


@pytest.mark.parametrize(
    "positions",
    [
        listing(),
        {"data": {"items": []}},
        {"data": {}},
        {"data": None},
        {"data": {"items": None}},
        {"data": {"items": "x"}},
        {"data": {"items": {"symbol": "AAPL"}}},
        {},
        [],
    ],
)
def test_sell_with_no_position_in_any_shape_is_refused(positions):
    router = tasty_router(positions=positions)
    client, _ = build_live(router)
    response = post(client, tasty(action="sell"))
    assert (response.status_code, response.json()) == (422, {"ok": False, "error": NO_LONG})
    assert router.sent("POST", ORDERS) == []


def test_sell_with_a_body_that_is_not_json_is_refused_as_no_position():
    router = tasty_router()
    router.on("GET", POSITIONS, reply(200, text="OK"))
    client, _ = build_live(router)
    assert post(client, tasty(action="sell")).json()["error"] == NO_LONG


@pytest.mark.parametrize(
    "item",
    [
        holding("AAPL", "100", "Short"),
        holding("AAPL", "0", "Zero"),
        holding("AAPL", "100", "long"),
        holding("AAPL", "100", None),
        holding("AAPL", "100", "Long", "Equity Option"),
        holding("AAPL", "100", "Long", "Cryptocurrency"),
        holding("MSFT", "100"),
        {"symbol": "AAPL", "quantity": "100"},
    ],
)
def test_sell_is_refused_unless_the_item_is_a_long_equity_in_that_symbol(item):
    router = tasty_router(positions=listing(item))
    client, _ = build_live(router)
    response = post(client, tasty(action="sell"))
    assert (response.status_code, response.json()["error"]) == (422, NO_LONG)
    assert router.sent("POST", ORDERS) == []


def test_sell_larger_than_the_position_is_refused():
    router = tasty_router(positions=listing(holding("AAPL", "3")))
    client, _ = build_live(router)
    response = post(client, tasty(action="sell", quantity=5))
    assert (response.status_code, response.json()) == (
        422,
        {"ok": False, "error": "sell quantity 5 exceeds the 3 shares held in AAPL"},
    )
    assert router.sent("POST", ORDERS) == []


def test_clamp_sells_trims_the_order_to_the_position_and_warns(caplog):
    caplog.set_level(logging.DEBUG)
    router = tasty_router(positions=listing(holding("AAPL", "3")))
    client, _ = build_live(router, {"CLAMP_SELLS": "true"})
    response = post(client, tasty(action="sell", quantity=5))
    assert response.status_code == 200
    assert response.json()["quantity"] == "3"
    body = body_json(sent_order(router))
    assert body["legs"][0]["quantity"] == "3" and body["legs"][0]["action"] == "Sell to Close"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == "tvbot"]
    assert "sell clamped from 5 to 3 for AAPL" in warnings


def test_a_decimal_string_quantity_reads_as_a_whole_number_in_messages_and_orders():
    router = tasty_router(positions=listing(holding("AAPL", "13.0")))
    client, _ = build_live(router)
    assert post(client, tasty(action="sell", quantity=20)).json()["error"] == "sell quantity 20 exceeds the 13 shares held in AAPL"

    router = tasty_router(positions=listing(holding("AAPL", "13.0")))
    client, _ = build_live(router, {"CLAMP_SELLS": "true"})
    assert post(client, tasty(action="sell", quantity=20)).json()["quantity"] == "13"
    assert body_json(sent_order(router))["legs"][0]["quantity"] == "13"


@pytest.mark.parametrize("held,sell", [("10", 5), ("5", 5), (5, 5), (5.0, 5)])
def test_sell_within_the_position_is_placed_unchanged(held, sell):
    router = tasty_router(positions=listing(holding("AAPL", held)))
    client, _ = build_live(router)
    assert post(client, tasty(action="sell", quantity=sell)).status_code == 200
    assert body_json(sent_order(router))["legs"][0]["quantity"] == str(sell)


def test_the_matching_item_is_picked_among_several():
    items = [holding("MSFT", "100"), holding("AAPL", "7", "Short"), holding("AAPL", "7", "Long", "Equity Option"), holding("AAPL", "6"), holding("SPY", "1")]
    router = tasty_router(positions=listing(*items))
    client, _ = build_live(router)
    assert post(client, tasty(action="sell", quantity=6)).status_code == 200
    assert post(client, tasty(action="sell", quantity=7)).status_code == 422


def test_a_clamped_order_is_validated_again():
    router = tasty_router(positions=listing(holding("AAPL", "2.5")))
    client, _ = build_live(router, {"CLAMP_SELLS": "true"})
    response = post(client, tasty(action="sell", quantity=3))
    assert (response.status_code, response.json()["error"]) == (422, "tastytrade orders must be whole shares in this bot")
    assert router.sent("POST", ORDERS) == []


@pytest.mark.parametrize(
    "answer,status,message",
    [
        (reply(403, json=envelope("forbidden", "Token scope does not allow positions")), 403, "Token scope does not allow positions"),
        (reply(404, json=envelope("not_found", "Account not found")), 404, "Account not found"),
        (reply(500, text="down"), 500, "down"),
    ],
)
def test_a_failed_position_read_is_a_424_and_no_order_is_sent(answer, status, message):
    router = tasty_router()
    router.on("GET", POSITIONS, answer)
    client, _ = build_live(router)
    response = post(client, tasty(action="sell"))
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": message, "broker_status": status})
    assert router.sent("POST", ORDERS) == []


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("bad")])
def test_a_transport_failure_on_the_position_read_is_a_424(exc):
    router = tasty_router()
    router.on("GET", POSITIONS, raising(exc))
    client, _ = build_live(router)
    response = post(client, tasty(action="sell"))
    assert response.status_code == 424 and response.json()["broker_status"] is None
    assert router.sent("POST", ORDERS) == []


@pytest.mark.parametrize("quantity", ["abc", None, "NaN", "Infinity"])
def test_an_unreadable_position_quantity_is_a_424(quantity):
    router = tasty_router(positions=listing(holding("AAPL", quantity)))
    client, _ = build_live(router)
    response = post(client, tasty(action="sell"))
    assert (response.status_code, response.json()["error"]) == (424, "unexpected position quantity from tastytrade")
    assert router.sent("POST", ORDERS) == []


def test_a_missing_position_quantity_is_a_424():
    item = holding()
    del item["quantity"]
    router = tasty_router(positions=listing(item))
    client, _ = build_live(router)
    assert post(client, tasty(action="sell")).json()["error"] == "unexpected position quantity from tastytrade"


def test_entries_that_are_not_objects_are_skipped():
    router = tasty_router(positions=listing("junk", 5, None, holding("AAPL", "9")))
    client, _ = build_live(router)
    assert post(client, tasty(action="sell", quantity=9)).status_code == 200


def test_held_quantity_returns_a_decimal_and_raises_on_a_non_2xx():
    router = Router().on("POST", TOKEN, token_reply()).on("GET", POSITIONS, reply(200, json=listing(holding("AAPL", "12.5"), holding("MSFT", "3"))))
    broker, _ = direct(router)
    value = broker.held_quantity("AAPL")
    assert isinstance(value, Decimal) and value == Decimal("12.5")
    assert broker.held_quantity("TSLA") == Decimal(0)

    failing = Router().on("POST", TOKEN, token_reply()).on("GET", POSITIONS, reply(403, json=envelope("forbidden", "no")))
    broker, _ = direct(failing)
    with pytest.raises(BrokerRejected) as caught:
        broker.held_quantity("AAPL")
    assert (caught.value.status_code, caught.value.message) == (403, "no")


def test_secrets_never_reach_the_logs_on_a_successful_sell(caplog):
    caplog.set_level(logging.DEBUG)
    router = tasty_router(positions=listing(holding()))
    client, _ = build_live(router)
    response = post(client, tasty(action="sell"))
    assert response.status_code == 200
    assert_no_secrets(caplog, response.text, secrets=[*ALL_SECRETS, "tasty-access-1"])
