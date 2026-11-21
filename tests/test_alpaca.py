"""Alpaca adapter: request shape, position reads, the sell guard and error text."""

from __future__ import annotations

import logging
import re
from decimal import Decimal

import pytest
from conftest import (
    ALPACA_KEY_ID,
    ALPACA_SECRET,
    Router,
    alert,
    assert_no_secrets,
    body_json,
    build,
    build_live,
    make_settings,
    reply,
)

from tvbot.brokers.alpaca import AlpacaBroker
from tvbot.brokers.base import BrokerRejected

CID = re.compile(r"tvbot-[0-9a-f]{24}")
NO_LONG = "no long position in AAPL to sell; this bot does not open short positions"


def post(client, payload):
    return client.post("/webhook", json=payload)


def order_router(order_reply=None, positions=None):
    router = Router().on("POST", "/v2/orders", order_reply or reply(200, json={"id": "alpaca-order-1"}))
    if positions is not None:
        router.on("GET", "/v2/positions", reply(200, json=positions))
    return router


def held(qty="10", available=None, symbol="AAPL", side="long"):
    item = {"symbol": symbol, "qty": qty, "side": side, "asset_class": "us_equity"}
    if available is not None:
        item["qty_available"] = available
    return item


def sent_order(router):
    [request] = router.sent("POST", "/v2/orders")
    return request


# request shape


def test_market_buy_request_matches_the_api_reference():
    router = order_router()
    client, _ = build_live(router)
    response = post(client, alert())
    request = sent_order(router)
    body = body_json(request)
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "dry_run": False,
        "broker": "alpaca",
        "order_id": "alpaca-order-1",
        "client_id": body["client_order_id"],
        "quantity": "1",
    }
    assert request.method == "POST"
    assert str(request.url) == "https://paper-api.alpaca.markets/v2/orders"
    assert request.headers["APCA-API-KEY-ID"] == ALPACA_KEY_ID
    assert request.headers["APCA-API-SECRET-KEY"] == ALPACA_SECRET
    assert CID.fullmatch(body["client_order_id"])
    assert body == {
        "symbol": "AAPL",
        "qty": "1",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": body["client_order_id"],
    }
    assert len(router.requests) == 1  # a buy reads no position


def test_limit_gtc_request_carries_a_string_price():
    router = order_router()
    client, _ = build_live(router)
    post(client, alert(quantity=3, order_type="limit", limit_price="187.50", time_in_force="gtc"))
    body = body_json(sent_order(router))
    assert body["qty"] == "3" and body["type"] == "limit" and body["time_in_force"] == "gtc"
    assert body["limit_price"] == "187.50"
    assert isinstance(body["qty"], str) and isinstance(body["limit_price"], str)


def test_a_market_order_has_no_limit_price_even_when_the_alert_names_one():
    router = order_router()
    client, _ = build_live(router)
    post(client, alert(limit_price=99))
    assert "limit_price" not in body_json(sent_order(router))


@pytest.mark.parametrize("quantity,expected", [(0.5, "0.5"), ("2.25", "2.25"), (1e-7, "0.0000001"), (3, "3")])
def test_fractional_market_day_orders_are_allowed_and_sent_as_plain_decimals(quantity, expected):
    router = order_router()
    client, _ = build_live(router)
    response = post(client, alert(quantity=quantity))
    assert response.status_code == 200
    assert body_json(sent_order(router))["qty"] == expected
    assert response.json()["quantity"] == expected


@pytest.mark.parametrize(
    "extra",
    [
        {"order_type": "limit", "limit_price": 10},
        {"time_in_force": "gtc"},
        {"order_type": "limit", "limit_price": 10, "time_in_force": "gtc"},
    ],
)
def test_fractional_quantity_is_refused_for_limit_and_gtc_orders(extra):
    router = order_router()
    client, _ = build_live(router)
    response = post(client, alert(quantity=0.5, **extra))
    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "Alpaca accepts fractional quantities only for market day orders"}
    assert router.requests == []


def test_whole_quantity_limit_gtc_is_fine():
    router = order_router()
    client, _ = build_live(router)
    assert post(client, alert(quantity=2, order_type="limit", limit_price=10, time_in_force="gtc")).status_code == 200


def test_paper_is_the_default_and_live_needs_the_flag():
    paper_router = order_router(positions=[held()])
    paper, _ = build_live(paper_router)
    post(paper, alert(action="sell"))
    assert str(sent_order(paper_router).url) == "https://paper-api.alpaca.markets/v2/orders"
    assert str(paper_router.sent("GET", "/v2/positions")[0].url) == "https://paper-api.alpaca.markets/v2/positions"

    live_router = order_router(positions=[held()])
    live, _ = build_live(live_router, {"ALPACA_LIVE": "true"})
    post(live, alert(action="sell"))
    assert str(sent_order(live_router).url) == "https://api.alpaca.markets/v2/orders"
    assert str(live_router.sent("GET", "/v2/positions")[0].url) == "https://api.alpaca.markets/v2/positions"


def test_dry_run_preview_url_follows_the_live_flag():
    paper, _ = build()
    live, _ = build(make_settings(ALPACA_LIVE="true"))
    assert post(paper, alert()).json()["request"]["url"] == "https://paper-api.alpaca.markets/v2/orders"
    assert post(live, alert()).json()["request"]["url"] == "https://api.alpaca.markets/v2/orders"


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"quantity": 2.5},
        {"order_type": "limit", "limit_price": 10.5, "time_in_force": "gtc", "quantity": 2},
        {"action": "sell", "quantity": 4},
        {"action": "sell", "order_type": "limit", "limit_price": "99.95", "quantity": 1},
    ],
)
def test_the_preview_equals_the_request_that_is_sent(extra):
    payload = alert(**extra)
    preview = post(build()[0], payload).json()["request"]

    router = order_router(positions=[held("10")])
    client, _ = build_live(router)
    assert post(client, payload).status_code == 200
    request = sent_order(router)

    sent_json = body_json(request)
    assert CID.fullmatch(preview["json"]["client_order_id"]) and CID.fullmatch(sent_json["client_order_id"])
    assert preview["json"]["client_order_id"] != sent_json["client_order_id"]
    assert (preview["method"], preview["url"]) == (request.method, str(request.url))
    assert {k: v for k, v in preview["json"].items() if k != "client_order_id"} == {
        k: v for k, v in sent_json.items() if k != "client_order_id"
    }


# responses


@pytest.mark.parametrize(
    "answer,expected",
    [
        (reply(200, json={}), None),
        (reply(200, json={"id": None}), None),
        (reply(200, text="ok"), None),
        (reply(201, json={"id": "61e69015-8549-4bfd-b9c3-01e75843f47d"}), "61e69015-8549-4bfd-b9c3-01e75843f47d"),
        (reply(200, json={"id": 12345}), "12345"),
        (reply(200, json=["not", "an", "object"]), None),
    ],
)
def test_a_2xx_answer_is_a_success_whatever_it_carries(answer, expected):
    client, _ = build_live(order_router(answer))
    response = post(client, alert())
    assert response.status_code == 200
    assert response.json()["order_id"] == expected


def test_recorded_buying_power_refusal_is_a_424_with_the_message_only():
    recorded = {"buying_power": "408185.8", "code": 40310000, "cost_basis": "10000030", "message": "insufficient buying power"}
    client, _ = build_live(order_router(reply(403, json=recorded)))
    response = post(client, alert())
    assert response.status_code == 424
    assert response.json() == {"ok": False, "error": "insufficient buying power", "broker_status": 403}


def test_recorded_sub_penny_refusal_is_a_424_with_the_message():
    message = "invalid limit_price 1.0001. sub-penny increment does not fulfill minimum pricing criteria"
    client, _ = build_live(order_router(reply(422, json={"code": 42210000, "message": message})))
    response = post(client, alert(order_type="limit", limit_price="1.0001"))
    assert response.status_code == 424
    assert response.json() == {"ok": False, "error": message, "broker_status": 422}


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429])
def test_any_4xx_is_a_424_that_carries_the_status(status):
    client, _ = build_live(order_router(reply(status, json={"message": "no"})))
    response = post(client, alert())
    assert (response.status_code, response.json()["broker_status"]) == (424, status)


@pytest.mark.parametrize(
    "answer,expected",
    [
        (reply(400, text="plain text refusal"), "plain text refusal"),
        (reply(400, json={"code": 1}), '{"code":1}'),
        (reply(400, json={"message": 5}), '{"message":5}'),
        (reply(400, json=["a"]), '["a"]'),
    ],
)
def test_an_error_body_without_a_message_falls_back_to_its_text(answer, expected):
    client, _ = build_live(order_router(answer))
    assert post(client, alert()).json()["error"] == expected


def test_a_redirect_is_not_a_success():
    client, _ = build_live(order_router(reply(302, text="moved")))
    response = post(client, alert())
    assert (response.status_code, response.json()["broker_status"]) == (424, 302)


# sells


def test_sell_with_no_position_is_refused_and_no_order_is_sent():
    router = order_router(positions=[])
    client, _ = build_live(router)
    response = post(client, alert(action="sell"))
    assert (response.status_code, response.json()) == (422, {"ok": False, "error": NO_LONG})
    assert router.sent("POST", "/v2/orders") == []
    assert len(router.sent("GET", "/v2/positions")) == 1


def test_sell_when_only_other_symbols_are_held_is_refused():
    router = order_router(positions=[held(symbol="MSFT"), held(symbol="SPY")])
    client, _ = build_live(router)
    assert post(client, alert(action="sell")).json()["error"] == NO_LONG
    assert router.sent("POST", "/v2/orders") == []


@pytest.mark.parametrize(
    "position",
    [
        {"symbol": "AAPL", "qty": "-5", "qty_available": "-5", "side": "short"},
        {"symbol": "AAPL", "qty": "5", "side": "short"},
        {"symbol": "AAPL", "qty": "5"},
        {"symbol": "AAPL", "qty": "5", "side": None},
    ],
)
def test_sell_against_a_short_position_is_refused(position):
    router = order_router(positions=[position])
    client, _ = build_live(router)
    response = post(client, alert(action="sell"))
    assert (response.status_code, response.json()["error"]) == (422, NO_LONG)
    assert router.sent("POST", "/v2/orders") == []


def test_sell_larger_than_the_position_is_refused():
    router = order_router(positions=[held("3")])
    client, _ = build_live(router)
    response = post(client, alert(action="sell", quantity=5))
    assert (response.status_code, response.json()) == (
        422,
        {"ok": False, "error": "sell quantity 5 exceeds the 3 shares held in AAPL"},
    )
    assert router.sent("POST", "/v2/orders") == []


def test_clamp_sells_trims_the_order_to_the_position_and_warns(caplog):
    caplog.set_level(logging.DEBUG)
    router = order_router(positions=[held("3")])
    client, _ = build_live(router, {"CLAMP_SELLS": "true"})
    response = post(client, alert(action="sell", quantity=5))
    assert response.status_code == 200
    assert response.json()["quantity"] == "3"
    body = body_json(sent_order(router))
    assert (body["side"], body["qty"]) == ("sell", "3")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == "tvbot"]
    assert "sell clamped from 5 to 3 for AAPL" in warnings


@pytest.mark.parametrize("held_qty,sell,expected", [("10", 5, "5"), ("5", 5, "5"), ("5.5", 5, "5")])
def test_sell_within_the_position_is_placed_unchanged(held_qty, sell, expected):
    router = order_router(positions=[held(held_qty)])
    client, _ = build_live(router)
    response = post(client, alert(action="sell", quantity=sell))
    assert response.status_code == 200
    assert body_json(sent_order(router))["qty"] == expected


def test_a_sell_is_matched_to_its_own_symbol_among_several_positions():
    router = order_router(positions=[held("100", symbol="MSFT"), held("7", symbol="AAPL"), held("1", symbol="SPY")])
    client, _ = build_live(router)
    assert post(client, alert(action="sell", quantity=7)).status_code == 200
    assert post(client, alert(action="sell", quantity=8)).status_code == 422


def test_shares_committed_to_open_orders_are_not_sellable():
    router = order_router(positions=[held("10", available="0")])
    client, _ = build_live(router)
    response = post(client, alert(action="sell"))
    assert (response.status_code, response.json()) == (
        422,
        {"ok": False, "error": "the shares held in AAPL are committed to open orders; none are available to sell"},
    )
    assert router.sent("POST", "/v2/orders") == []


def test_the_available_quantity_is_what_counts():
    router = order_router(positions=[held("10", available="4")])
    client, _ = build_live(router)
    assert post(client, alert(action="sell", quantity=5)).json()["error"] == (
        "sell quantity 5 exceeds the 4 shares held in AAPL"
    )

    router = order_router(positions=[held("10", available="4")])
    client, _ = build_live(router, {"CLAMP_SELLS": "true"})
    assert post(client, alert(action="sell", quantity=5)).status_code == 200
    assert body_json(sent_order(router))["qty"] == "4"


def test_a_null_available_quantity_falls_back_to_the_position_quantity():
    router = order_router(positions=[{"symbol": "AAPL", "qty": "6", "side": "long", "qty_available": None}])
    client, _ = build_live(router)
    assert post(client, alert(action="sell", quantity=6)).status_code == 200


def test_a_clamped_order_is_validated_again():
    router = order_router(positions=[held("2.5")])
    client, _ = build_live(router, {"CLAMP_SELLS": "true"})
    response = post(client, alert(action="sell", quantity=3, order_type="limit", limit_price=10, time_in_force="gtc"))
    assert response.status_code == 422
    assert response.json()["error"] == "Alpaca accepts fractional quantities only for market day orders"
    assert router.sent("POST", "/v2/orders") == []


def test_a_fractional_position_can_be_sold_whole_with_a_market_day_order():
    router = order_router(positions=[held("2.5")])
    client, _ = build_live(router, {"CLAMP_SELLS": "true"})
    response = post(client, alert(action="sell", quantity=3))
    assert response.status_code == 200
    assert body_json(sent_order(router))["qty"] == "2.5"


def test_the_position_read_sends_the_credentials_and_nothing_else():
    router = order_router(positions=[held()])
    client, _ = build_live(router)
    post(client, alert(action="sell"))
    [request] = router.sent("GET", "/v2/positions")
    assert request.headers["APCA-API-KEY-ID"] == ALPACA_KEY_ID
    assert request.headers["APCA-API-SECRET-KEY"] == ALPACA_SECRET
    assert request.content == b""


@pytest.mark.parametrize(
    "answer,status,message",
    [
        (reply(401, json={"message": "forbidden."}), 401, "forbidden."),
        (reply(403, json={"message": "request is not authorized"}), 403, "request is not authorized"),
        (reply(422, text="bad request"), 422, "bad request"),
    ],
)
def test_a_failed_position_read_is_a_424_and_no_order_is_sent(answer, status, message):
    router = Router().on("GET", "/v2/positions", answer)
    router.on("POST", "/v2/orders", reply(200, json={"id": "must-not-happen"}))
    client, _ = build_live(router)
    response = post(client, alert(action="sell"))
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": message, "broker_status": status})
    assert router.sent("POST", "/v2/orders") == []


def test_a_server_error_on_the_position_read_is_a_424_not_a_504():
    router = Router().on("GET", "/v2/positions", reply(503, text="unavailable"))
    client, _ = build_live(router)
    response = post(client, alert(action="sell"))
    assert (response.status_code, response.json()["broker_status"]) == (424, 503)


@pytest.mark.parametrize("body", [{"message": "x"}, "text", None, 5])
def test_a_positions_answer_that_is_not_a_list_is_a_424(body):
    router = Router().on("GET", "/v2/positions", reply(200, json=body) if body is not None else reply(200, text="null"))
    client, _ = build_live(router)
    response = post(client, alert(action="sell"))
    assert (response.status_code, response.json()["error"]) == (424, "unexpected positions response from Alpaca")


@pytest.mark.parametrize(
    "position",
    [
        {"symbol": "AAPL", "qty": "abc", "side": "long"},
        {"symbol": "AAPL", "qty": "NaN", "side": "long"},
        {"symbol": "AAPL", "qty": None, "side": "long"},
        {"symbol": "AAPL", "qty": "5", "side": "long", "qty_available": "inf"},
        {"symbol": "AAPL", "qty": "5", "side": "long", "qty_available": "x"},
    ],
)
def test_an_unreadable_position_quantity_is_a_424(position):
    router = order_router(positions=[position])
    client, _ = build_live(router)
    response = post(client, alert(action="sell"))
    assert (response.status_code, response.json()["error"]) == (424, "unexpected position quantity from Alpaca")
    assert router.sent("POST", "/v2/orders") == []


def test_entries_that_are_not_objects_are_skipped():
    router = order_router(positions=["junk", 5, None, held("9")])
    client, _ = build_live(router)
    assert post(client, alert(action="sell", quantity=9)).status_code == 200


def test_held_quantity_returns_a_decimal_and_raises_on_a_non_200():
    router = Router().on("GET", "/v2/positions", reply(200, json=[held("12.5"), held("3", symbol="MSFT")]))
    broker = AlpacaBroker(make_settings(), router.client())
    value = broker.held_quantity("AAPL")
    assert isinstance(value, Decimal) and value == Decimal("12.5")
    assert broker.held_quantity("TSLA") == Decimal(0)

    failing = AlpacaBroker(make_settings(), Router().on("GET", "/v2/positions", reply(401, json={"message": "no"})).client())
    with pytest.raises(BrokerRejected) as caught:
        failing.held_quantity("AAPL")
    assert caught.value.status_code == 401


def test_credentials_never_reach_the_logs_on_a_successful_order(caplog):
    caplog.set_level(logging.DEBUG)
    router = order_router(positions=[held()])
    client, _ = build_live(router)
    response = post(client, alert(action="sell"))
    assert response.status_code == 200
    assert_no_secrets(caplog, response.text)
