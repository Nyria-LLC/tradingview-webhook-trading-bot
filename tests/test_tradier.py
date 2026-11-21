"""Tradier adapter: request shape, position reads, the sell guard and error text."""

from __future__ import annotations

import logging
import re
from decimal import Decimal

import httpx
import pytest
from conftest import (
    TRADIER_ACCOUNT,
    TRADIER_TOKEN,
    Router,
    alert,
    assert_no_secrets,
    body_form,
    build,
    build_live,
    make_settings,
    raising,
    reply,
)

from tvbot.brokers.base import BrokerRejected
from tvbot.brokers.tradier import TradierBroker, errors_text

ORDERS = f"/v1/accounts/{TRADIER_ACCOUNT}/orders"
POSITIONS = f"/v1/accounts/{TRADIER_ACCOUNT}/positions"
TAG = re.compile(r"tvbot-[0-9a-f]{24}")
NO_LONG = "no long position in AAPL to sell; this bot does not open short positions"
OK_ORDER = {"order": {"id": 257459, "status": "ok", "partner_id": "c4998eb7-06e8-4820-a7ab-55d9760065fb"}}


def post(client, payload):
    return client.post("/webhook", json=payload)


def tradier(**extra):
    return alert(broker="tradier", **extra)


def order_router(order_reply=None, positions=None):
    router = Router().on("POST", ORDERS, order_reply or reply(200, json=OK_ORDER))
    if positions is not None:
        router.on("GET", POSITIONS, reply(200, json=positions))
    return router


def listed(*items):
    return {"positions": {"position": list(items)}}


def lot(symbol="AAPL", quantity=10):
    return {"cost_basis": 207.01, "date_acquired": "2018-08-08T14:41:11.405Z", "id": 130089, "quantity": quantity, "symbol": symbol}


def sent_order(router):
    [request] = router.sent("POST", ORDERS)
    return request


# request shape


def test_market_buy_request_matches_the_api_reference():
    router = order_router()
    client, _ = build_live(router)
    response = post(client, tradier())
    request = sent_order(router)
    form = body_form(request)
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "dry_run": False,
        "broker": "tradier",
        "order_id": "257459",
        "client_id": form["tag"],
        "quantity": "1",
    }
    assert request.method == "POST"
    assert str(request.url) == f"https://sandbox.tradier.com/v1/accounts/{TRADIER_ACCOUNT}/orders"
    assert request.headers["Authorization"] == f"Bearer {TRADIER_TOKEN}"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert TAG.fullmatch(form["tag"])
    assert form == {
        "class": "equity",
        "symbol": "AAPL",
        "side": "buy",
        "quantity": "1",
        "type": "market",
        "duration": "day",
        "tag": form["tag"],
    }
    assert len(router.requests) == 1  # a buy reads no position


def test_the_tag_uses_only_characters_tradier_accepts():
    router = order_router()
    client, _ = build_live(router)
    post(client, tradier())
    assert re.fullmatch(r"[A-Za-z0-9-]{1,255}", body_form(sent_order(router))["tag"])


def test_limit_gtc_request_carries_the_price():
    router = order_router()
    client, _ = build_live(router)
    post(client, tradier(quantity=3, order_type="limit", limit_price="187.50", time_in_force="gtc"))
    form = body_form(sent_order(router))
    assert (form["quantity"], form["type"], form["duration"], form["price"]) == ("3", "limit", "gtc", "187.50")


def test_a_market_order_has_no_price_even_when_the_alert_names_one():
    router = order_router()
    client, _ = build_live(router)
    post(client, tradier(limit_price=99))
    assert "price" not in body_form(sent_order(router))


@pytest.mark.parametrize("quantity,expected", [(2.0, "2"), ("3", "3"), (100, "100"), ("4.000", "4")])
def test_whole_quantities_are_sent_as_integers(quantity, expected):
    router = order_router()
    client, _ = build_live(router)
    response = post(client, tradier(quantity=quantity))
    assert response.status_code == 200
    assert body_form(sent_order(router))["quantity"] == expected


@pytest.mark.parametrize("quantity", [0.5, "2.25", 1.0000001])
def test_fractional_quantity_is_refused_before_anything_is_sent(quantity):
    router = order_router()
    client, _ = build_live(router)
    response = post(client, tradier(quantity=quantity))
    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "Tradier orders must be whole shares"}
    assert router.requests == []


def test_fractional_quantity_is_refused_in_a_dry_run_too():
    client, _ = build()
    response = post(client, tradier(quantity=0.5))
    assert (response.status_code, response.json()["error"]) == (422, "Tradier orders must be whole shares")


def test_sandbox_is_the_default_and_live_needs_the_flag():
    sandbox_router = order_router(positions=listed(lot()))
    sandbox, _ = build_live(sandbox_router)
    post(sandbox, tradier(action="sell"))
    assert str(sent_order(sandbox_router).url).startswith("https://sandbox.tradier.com/v1/accounts/")
    assert str(sandbox_router.sent("GET", POSITIONS)[0].url) == (
        f"https://sandbox.tradier.com/v1/accounts/{TRADIER_ACCOUNT}/positions"
    )

    live_router = order_router(positions=listed(lot()))
    live, _ = build_live(live_router, {"TRADIER_LIVE": "true"})
    post(live, tradier(action="sell"))
    assert str(sent_order(live_router).url) == f"https://api.tradier.com/v1/accounts/{TRADIER_ACCOUNT}/orders"
    assert str(live_router.sent("GET", POSITIONS)[0].url) == (
        f"https://api.tradier.com/v1/accounts/{TRADIER_ACCOUNT}/positions"
    )


def test_the_account_id_is_percent_encoded_in_the_path():
    settings = make_settings(DRY_RUN="false", DEDUPE_SECONDS="0", TRADIER_ACCOUNT_ID="VA 0/1?x")
    router = Router().on("POST", "/v1/accounts/VA 0/1?x/orders", reply(200, json=OK_ORDER))
    client, _ = build(settings, router)
    assert post(client, tradier()).status_code == 200
    [request] = router.requests
    assert request.url.raw_path == b"/v1/accounts/VA%200%2F1%3Fx/orders"


def test_dry_run_preview_uses_a_placeholder_and_the_flag_picks_the_host():
    sandbox, _ = build()
    live, _ = build(make_settings(TRADIER_LIVE="true"))
    one = post(sandbox, tradier()).json()
    two = post(live, tradier()).json()
    assert one["dry_run"] is True
    assert one["request"]["url"] == "https://sandbox.tradier.com/v1/accounts/<TRADIER_ACCOUNT_ID>/orders"
    assert two["request"]["url"] == "https://api.tradier.com/v1/accounts/<TRADIER_ACCOUNT_ID>/orders"
    assert TRADIER_ACCOUNT not in one["request"]["url"]
    assert one["request"]["method"] == "POST"


def test_a_dry_run_makes_no_network_call_even_for_a_sell():
    router = Router()
    client, _ = build(router=router)
    response = post(client, tradier(action="sell"))
    assert response.status_code == 200
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
    payload = tradier(**extra)
    preview = post(build()[0], payload).json()["request"]

    router = order_router(positions=listed(lot("AAPL", 10)))
    client, _ = build_live(router)
    assert post(client, payload).status_code == 200
    request = sent_order(router)

    sent_form = body_form(request)
    assert TAG.fullmatch(preview["form"]["tag"]) and TAG.fullmatch(sent_form["tag"])
    assert preview["form"]["tag"] != sent_form["tag"]
    assert preview["method"] == request.method
    assert preview["url"] == str(request.url).replace(TRADIER_ACCOUNT, "<TRADIER_ACCOUNT_ID>")
    assert {k: v for k, v in preview["form"].items() if k != "tag"} == {k: v for k, v in sent_form.items() if k != "tag"}


# responses


@pytest.mark.parametrize(
    "body,expected",
    [
        (OK_ORDER, "257459"),
        ({"order": {"id": 123456, "status": "ok", "partner_id": "partner_12345"}}, "123456"),
        ({"order": {"id": "abc-1", "status": "ok"}}, "abc-1"),
        ({"order": {"id": 5, "status": "pending"}}, None),
        ({"order": {"status": "ok"}}, None),
        ({"order": {"id": None, "status": "ok"}}, None),
        ({"order": {}}, None),
    ],
)
def test_order_id_is_reported_only_for_an_ok_order_that_has_one(body, expected):
    client, _ = build_live(order_router(reply(200, json=body)))
    response = post(client, tradier())
    assert response.status_code == 200
    assert response.json()["order_id"] == expected


@pytest.mark.parametrize(
    "errors,expected",
    [
        ({"error": ["Order rejected by risk", "second reason"]}, "Order rejected by risk; second reason"),
        ({"error": ["only one"]}, "only one"),
        ({"error": "Account is disabled for trading"}, "Account is disabled for trading"),
        ("plain text", "plain text"),
        ({"other": 1}, "{'other': 1}"),
        (["a", "b"], "['a', 'b']"),
        ({"error": []}, ""),
    ],
)
def test_a_200_with_errors_is_a_424_carrying_the_error_text(errors, expected):
    client, _ = build_live(order_router(reply(200, json={"errors": errors})))
    response = post(client, tradier())
    assert response.status_code == 424
    assert response.json() == {"ok": False, "error": expected, "broker_status": 200}


def test_errors_win_over_an_order_in_the_same_answer():
    body = {"errors": {"error": ["refused"]}, "order": {"id": 1, "status": "ok"}}
    client, _ = build_live(order_router(reply(200, json=body)))
    response = post(client, tradier())
    assert (response.status_code, response.json()["error"]) == (424, "refused")


@pytest.mark.parametrize(
    "answer",
    [
        reply(200, json={}),
        reply(200, json=[]),
        reply(200, json="text"),
        reply(200, json={"order": "x"}),
        reply(200, json={"order": None}),
        reply(200, json={"order": [1]}),
        reply(200, text="not json"),
        reply(204),
    ],
)
def test_an_answer_without_an_order_object_is_a_424(answer):
    client, _ = build_live(order_router(answer))
    response = post(client, tradier())
    assert response.status_code == 424
    assert response.json()["error"] == "unexpected response from Tradier"


@pytest.mark.parametrize(
    "answer,status,message",
    [
        (reply(400, json={"errors": {"error": ["Invalid parameter"]}}), 400, "Invalid parameter"),
        (reply(401, text="Invalid Access Token"), 401, "Invalid Access Token"),
        (reply(403, json={"errors": {"error": "Account access denied"}}), 403, "Account access denied"),
        (reply(404, json={"unrelated": 1}), 404, '{"unrelated":1}'),
        (reply(429, text="slow down"), 429, "slow down"),
    ],
)
def test_any_4xx_is_a_424_that_carries_the_status_and_message(answer, status, message):
    client, _ = build_live(order_router(answer))
    response = post(client, tradier())
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": message, "broker_status": status})


@pytest.mark.parametrize("status", [500, 502, 503, 599])
def test_a_5xx_on_the_order_is_a_504_naming_the_tag(status):
    router = order_router(reply(status, text="upstream"))
    client, _ = build_live(router, {"DEDUPE_SECONDS": "30"})
    response = post(client, tradier())
    tag = body_form(sent_order(router))["tag"]
    assert response.status_code == 504
    assert response.json() == {
        "ok": False,
        "error": f"outcome unknown (tradier returned HTTP {status}); the order may have been placed. Check your broker for client id {tag}",
    }
    assert post(client, tradier()).json() == {"ok": True, "duplicate": True}  # the same alert is not resent
    assert len(router.sent("POST", ORDERS)) == 1


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("slow"), httpx.WriteTimeout("slow"), httpx.ReadError("cut"), httpx.RemoteProtocolError("bad")]
)
def test_a_transport_failure_after_the_request_may_have_left_is_a_504(exc):
    router = Router().on("POST", ORDERS, raising(exc))
    client, _ = build_live(router)
    response = post(client, tradier())
    tag = body_form(sent_order(router))["tag"]
    assert response.status_code == 504
    assert response.json() == {
        "ok": False,
        "error": f"outcome unknown ({type(exc).__name__}); the order may have been placed. Check your broker for client id {tag}",
    }


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("busy")])
def test_a_failure_before_the_request_left_is_a_424(exc):
    router = Router().on("POST", ORDERS, raising(exc))
    client, _ = build_live(router, {"DEDUPE_SECONDS": "30"})
    response = post(client, tradier())
    assert (response.status_code, response.json()) == (
        424,
        {"ok": False, "error": f"could not reach tradier: {type(exc).__name__}", "broker_status": None},
    )
    router.on("POST", ORDERS, reply(200, json=OK_ORDER))
    assert post(client, tradier()).status_code == 200  # a refused attempt can be retried at once


# sells


@pytest.mark.parametrize(
    "positions",
    [
        {"positions": "null"},
        {"positions": None},
        {"positions": {}},
        {"positions": {"position": []}},
        {"positions": {"position": None}},
        {"positions": {"position": "x"}},
        {},
        [],
    ],
)
def test_sell_with_no_position_in_any_shape_is_refused(positions):
    router = order_router(positions=positions)
    client, _ = build_live(router)
    response = post(client, tradier(action="sell"))
    assert (response.status_code, response.json()) == (422, {"ok": False, "error": NO_LONG})
    assert router.sent("POST", ORDERS) == []


def test_sell_with_a_body_that_is_not_json_is_refused_as_no_position():
    router = Router().on("GET", POSITIONS, reply(200, text="OK"))
    client, _ = build_live(router)
    response = post(client, tradier(action="sell"))
    assert (response.status_code, response.json()["error"]) == (422, NO_LONG)


def test_sell_when_only_other_symbols_are_held_is_refused():
    router = order_router(positions=listed(lot("MSFT", 3), lot("SPY", 9)))
    client, _ = build_live(router)
    assert post(client, tradier(action="sell")).json()["error"] == NO_LONG
    assert router.sent("POST", ORDERS) == []


def test_sell_against_a_short_position_is_refused():
    router = order_router(positions=listed(lot("AAPL", -2), lot("NFLX", 37)))
    client, _ = build_live(router)
    response = post(client, tradier(action="sell"))
    assert (response.status_code, response.json()["error"]) == (422, NO_LONG)
    assert router.sent("POST", ORDERS) == []


def test_sell_larger_than_the_position_is_refused():
    router = order_router(positions=listed(lot("AAPL", 3)))
    client, _ = build_live(router)
    response = post(client, tradier(action="sell", quantity=5))
    assert (response.status_code, response.json()) == (
        422,
        {"ok": False, "error": "sell quantity 5 exceeds the 3 shares held in AAPL"},
    )
    assert router.sent("POST", ORDERS) == []


def test_clamp_sells_trims_the_order_to_the_position_and_warns(caplog):
    caplog.set_level(logging.DEBUG)
    router = order_router(positions=listed(lot("AAPL", 3)))
    client, _ = build_live(router, {"CLAMP_SELLS": "true"})
    response = post(client, tradier(action="sell", quantity=5))
    assert response.status_code == 200
    assert response.json()["quantity"] == "3"
    form = body_form(sent_order(router))
    assert (form["side"], form["quantity"]) == ("sell", "3")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == "tvbot"]
    assert "sell clamped from 5 to 3 for AAPL" in warnings


def test_a_json_float_quantity_reads_as_a_whole_number_in_messages_and_orders(caplog):
    caplog.set_level(logging.DEBUG)
    router = order_router(positions=listed(lot("AAPL", 13.0)))
    client, _ = build_live(router)
    assert post(client, tradier(action="sell", quantity=20)).json()["error"] == (
        "sell quantity 20 exceeds the 13 shares held in AAPL"
    )

    router = order_router(positions=listed(lot("AAPL", 13.0)))
    client, _ = build_live(router, {"CLAMP_SELLS": "true"})
    response = post(client, tradier(action="sell", quantity=20))
    assert response.status_code == 200 and response.json()["quantity"] == "13"
    assert body_form(sent_order(router))["quantity"] == "13"
    assert any(r.getMessage() == "sell clamped from 20 to 13 for AAPL" for r in caplog.records)


@pytest.mark.parametrize("held,sell", [(10, 5), (5, 5), (5.0, 5)])
def test_sell_within_the_position_is_placed_unchanged(held, sell):
    router = order_router(positions=listed(lot("AAPL", held)))
    client, _ = build_live(router)
    response = post(client, tradier(action="sell", quantity=sell))
    assert response.status_code == 200
    assert body_form(sent_order(router))["quantity"] == str(sell)


def test_a_single_position_comes_back_as_one_object():
    router = order_router(positions={"positions": {"position": lot("AAPL", 8)}})
    client, _ = build_live(router)
    assert post(client, tradier(action="sell", quantity=8)).status_code == 200
    assert post(client, tradier(action="sell", quantity=9)).status_code == 422


def test_the_documented_two_position_example_is_read_per_symbol():
    example = {
        "positions": {
            "position": [
                {"cost_basis": 7954.16, "date_acquired": "2015-01-01T15:25:47.230Z", "id": 40, "quantity": 37, "symbol": "AAPL"},
                {"cost_basis": 82.75, "date_acquired": "2014-09-01T15:25:47.230Z", "id": 41, "quantity": -2, "symbol": "NFLX"},
            ]
        }
    }
    router = order_router(positions=example)
    client, _ = build_live(router)
    assert post(client, tradier(action="sell", quantity=37)).status_code == 200
    assert post(client, tradier(action="sell", quantity=1)).status_code == 200
    assert post(client, tradier(action="sell", quantity=38)).status_code == 422
    nflx = post(client, tradier(action="sell", symbol="NFLX"))
    assert (nflx.status_code, nflx.json()["error"]) == (422, NO_LONG.replace("AAPL", "NFLX"))


def test_a_string_quantity_in_the_position_is_read():
    router = order_router(positions=listed(lot("AAPL", "6")))
    client, _ = build_live(router)
    assert post(client, tradier(action="sell", quantity=6)).status_code == 200


def test_a_clamped_order_is_validated_again():
    router = order_router(positions=listed(lot("AAPL", 2.5)))
    client, _ = build_live(router, {"CLAMP_SELLS": "true"})
    response = post(client, tradier(action="sell", quantity=3))
    assert (response.status_code, response.json()["error"]) == (422, "Tradier orders must be whole shares")
    assert router.sent("POST", ORDERS) == []


def test_entries_that_are_not_objects_are_skipped():
    router = order_router(positions={"positions": {"position": ["junk", 5, None, lot("AAPL", 9)]}})
    client, _ = build_live(router)
    assert post(client, tradier(action="sell", quantity=9)).status_code == 200


def test_the_position_read_sends_the_token_and_nothing_else():
    router = order_router(positions=listed(lot()))
    client, _ = build_live(router)
    post(client, tradier(action="sell"))
    [request] = router.sent("GET", POSITIONS)
    assert request.headers["Authorization"] == f"Bearer {TRADIER_TOKEN}"
    assert request.headers["Accept"] == "application/json"
    assert request.content == b""


@pytest.mark.parametrize(
    "answer,status,message",
    [
        (reply(401, text="Invalid Access Token"), 401, "Invalid Access Token"),
        (reply(404, json={"errors": {"error": ["Account not found"]}}), 404, "Account not found"),
        (reply(503, text="unavailable"), 503, "unavailable"),
    ],
)
def test_a_failed_position_read_is_a_424_and_no_order_is_sent(answer, status, message):
    router = Router().on("GET", POSITIONS, answer)
    router.on("POST", ORDERS, reply(200, json=OK_ORDER))
    client, _ = build_live(router)
    response = post(client, tradier(action="sell"))
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": message, "broker_status": status})
    assert router.sent("POST", ORDERS) == []


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("bad")]
)
def test_a_transport_failure_on_the_position_read_is_a_424(exc):
    router = Router().on("GET", POSITIONS, raising(exc))
    client, _ = build_live(router)
    response = post(client, tradier(action="sell"))
    assert response.status_code == 424
    assert response.json()["broker_status"] is None
    assert router.sent("POST", ORDERS) == []


@pytest.mark.parametrize(
    "item",
    [
        {"symbol": "AAPL", "quantity": "abc"},
        {"symbol": "AAPL", "quantity": None},
        {"symbol": "AAPL", "quantity": "NaN"},
        {"symbol": "AAPL", "quantity": "Infinity"},
        {"symbol": "AAPL"},
    ],
)
def test_an_unreadable_position_quantity_is_a_424(item):
    router = order_router(positions=listed(item))
    client, _ = build_live(router)
    response = post(client, tradier(action="sell"))
    assert (response.status_code, response.json()["error"]) == (424, "unexpected position quantity from Tradier")
    assert router.sent("POST", ORDERS) == []


def test_held_quantity_returns_a_decimal_and_raises_on_a_non_2xx():
    body = listed(lot("AAPL", 12.5), lot("MSFT", 3), lot("NFLX", -2))
    broker = TradierBroker(make_settings(), Router().on("GET", POSITIONS, reply(200, json=body)).client())
    value = broker.held_quantity("AAPL")
    assert isinstance(value, Decimal) and value == Decimal("12.5")
    assert broker.held_quantity("TSLA") == Decimal(0)
    assert broker.held_quantity("NFLX") == Decimal(0)

    failing = TradierBroker(make_settings(), Router().on("GET", POSITIONS, reply(401, text="no")).client())
    with pytest.raises(BrokerRejected) as caught:
        failing.held_quantity("AAPL")
    assert (caught.value.status_code, caught.value.message) == (401, "no")


@pytest.mark.parametrize(
    "errors,expected",
    [
        ({"error": ["a", "b"]}, "a; b"),
        ({"error": "one"}, "one"),
        ({"error": 5}, "{'error': 5}"),
        ({}, "{}"),
        (None, "None"),
        (["x"], "['x']"),
        ("text", "text"),
    ],
)
def test_errors_text_joins_what_tradier_returns(errors, expected):
    assert errors_text(errors) == expected


def test_the_token_and_account_never_reach_the_logs_or_responses(caplog):
    caplog.set_level(logging.DEBUG)
    router = order_router(positions=listed(lot()))
    client, _ = build_live(router)
    response = post(client, tradier(action="sell"))
    assert response.status_code == 200
    assert_no_secrets(caplog, response.text)

    failing = order_router(reply(200, json={"errors": {"error": ["refused"]}}), positions=listed(lot()))
    client, _ = build_live(failing)
    response = post(client, tradier(action="sell"))
    assert response.status_code == 424
    assert_no_secrets(caplog, response.text)
