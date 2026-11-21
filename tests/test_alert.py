"""Alert validation: the shared secret check and the field rules of the webhook contract."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tvbot.alert import AlertError, check_secret, parse_alert
from tvbot.brokers.base import OrderIntent, dec_str
from tvbot.config import load_settings

SECRET = "test-webhook-secret-0123456789abcdef"
ROOT = Path(__file__).resolve().parents[1]


def settings(**env: str):
    return load_settings({"WEBHOOK_SECRET": SECRET, **env})


def payload(**overrides):
    base = {"secret": SECRET, "symbol": "AAPL", "action": "buy", "quantity": 1}
    base.update(overrides)
    return base


def parse(overrides=None, **env):
    return parse_alert(payload(**(overrides or {})), settings(**env))


def refusal(overrides=None, **env) -> str:
    with pytest.raises(AlertError) as caught:
        parse(overrides, **env)
    assert caught.value.status == 422
    return caught.value.message


# secret


def test_correct_secret_passes():
    check_secret(payload(), settings())


@pytest.mark.parametrize(
    "supplied",
    ["", "wrong", SECRET + "x", SECRET[:-1], SECRET.upper(), " " + SECRET, "\ud800", "é" * len(SECRET)],
)
def test_wrong_secret_is_a_401(supplied):
    with pytest.raises(AlertError) as caught:
        check_secret(payload(secret=supplied), settings())
    assert (caught.value.status, caught.value.message) == (401, "unauthorized")


@pytest.mark.parametrize("supplied", [None, 123, 1.5, True, ["a"], {"a": 1}])
def test_non_string_secret_is_a_401(supplied):
    with pytest.raises(AlertError) as caught:
        check_secret(payload(secret=supplied), settings())
    assert caught.value.status == 401


def test_missing_secret_is_a_401():
    body = payload()
    del body["secret"]
    with pytest.raises(AlertError) as caught:
        check_secret(body, settings())
    assert caught.value.status == 401


def test_secret_with_non_ascii_characters_compares_exactly():
    secret = "geheim-schlüssel-0123456789"
    s = load_settings({"WEBHOOK_SECRET": secret})
    check_secret({"secret": secret}, s)
    with pytest.raises(AlertError):
        check_secret({"secret": "geheim-schlussel-0123456789"}, s)


# a valid alert


def test_minimal_alert_gets_the_defaults():
    intent = parse()
    assert intent == OrderIntent(
        broker="alpaca",
        symbol="AAPL",
        side="buy",
        quantity=Decimal(1),
        order_type="market",
        limit_price=None,
        time_in_force="day",
    )


def test_full_limit_alert():
    intent = parse(
        {
            "broker": "Tradier",
            "symbol": "msft",
            "action": "SELL",
            "quantity": "3",
            "order_type": "LIMIT",
            "limit_price": 410.5,
            "time_in_force": "GTC",
        }
    )
    assert intent == OrderIntent("tradier", "MSFT", "sell", Decimal(3), "limit", Decimal("410.5"), "gtc")


def test_default_broker_comes_from_the_settings():
    assert parse(DEFAULT_BROKER="schwab").broker == "schwab"
    assert parse({"broker": "tastytrade"}, DEFAULT_BROKER="schwab").broker == "tastytrade"


def test_alert_id_and_unknown_fields_are_ignored():
    intent = parse({"alert_id": "2026-11-20T00:00:00Z", "comment": "hello", "extra": {"a": [1]}})
    assert intent.symbol == "AAPL"


def test_example_files_pass_after_placeholder_substitution():
    substitutions = {
        "{{ticker}}": "AAPL",
        "{{timenow}}": "2026-11-20T00:00:00Z",
        "{{strategy.order.action}}": "buy",
        "{{strategy.order.contracts}}": "1",
        "paste-your-WEBHOOK_SECRET-here": SECRET,
    }
    for name in ("tradingview-alert-indicator.json", "tradingview-alert-strategy.json"):
        text = (ROOT / "examples" / name).read_text(encoding="utf-8")
        for placeholder, value in substitutions.items():
            text = text.replace(placeholder, value)
        body = json.loads(text)
        check_secret(body, settings())
        intent = parse_alert(body, settings())
        assert (intent.broker, intent.symbol, intent.side, intent.quantity) == ("alpaca", "AAPL", "buy", Decimal(1))


# broker


@pytest.mark.parametrize("value", ["interactive", "", 7, None, ["alpaca"], True])
def test_unknown_broker(value):
    assert refusal({"broker": value}) == "unknown broker"


@pytest.mark.parametrize("value,expected", [("ALPACA", "alpaca"), (" Schwab ", "schwab")])
def test_broker_is_lower_cased_and_trimmed(value, expected):
    assert parse({"broker": value}).broker == expected


# symbol


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AAPL", "AAPL"),
        ("aapl", "AAPL"),
        ("  spy  ", "SPY"),
        (" AAPL\n", "AAPL"),
        ("NASDAQ:AAPL", "AAPL"),
        ("BATS:NVDA", "NVDA"),
        ("nyse:brk.b", "BRK.B"),
        ("A:B:C", "C"),
        ("X" * 10, "X" * 10),
    ],
)
def test_symbol_normalisation(raw, expected):
    assert parse({"symbol": raw}).symbol == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "1ABC", "AAPL!", "AA PL", "X" * 11, "ÀAPL", "AAPŁ", "$AAPL", ".AAPL", "NASDAQ:", ":", None, 5, ["AAPL"]],
)
def test_invalid_symbol(raw):
    assert refusal({"symbol": raw}) == "invalid symbol"


def test_missing_symbol():
    body = payload()
    del body["symbol"]
    with pytest.raises(AlertError) as caught:
        parse_alert(body, settings())
    assert (caught.value.status, caught.value.message) == (422, "invalid symbol")


def test_allowed_symbols_filter_after_normalisation():
    assert parse({"symbol": "nasdaq:aapl"}, ALLOWED_SYMBOLS="aapl,msft").symbol == "AAPL"
    assert refusal({"symbol": "TSLA"}, ALLOWED_SYMBOLS="aapl,msft") == "symbol not allowed"


# action


@pytest.mark.parametrize("raw,expected", [("buy", "buy"), ("BUY", "buy"), (" Sell ", "sell")])
def test_action_normalisation(raw, expected):
    assert parse({"action": raw}).side == expected


@pytest.mark.parametrize("raw", ["", "hold", "close", "buy!", None, 1, True, ["buy"]])
def test_invalid_action(raw):
    assert refusal({"action": raw}) == "action must be buy or sell"


def test_missing_action():
    body = payload()
    del body["action"]
    with pytest.raises(AlertError) as caught:
        parse_alert(body, settings())
    assert caught.value.message == "action must be buy or sell"


# quantity


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1, "1"),
        (25, "25"),
        (1.5, "1.5"),
        (0.25, "0.25"),
        ("2", "2"),
        ("2.50", "2.50"),
        (" 2 ", "2"),
        ("\t3\n", "3"),
        ("007", "7"),
        (100, "100"),
        ("100.0", "100.0"),
    ],
)
def test_quantity_forms_that_are_accepted(raw, expected):
    assert parse({"quantity": raw}).quantity == Decimal(expected)


@pytest.mark.parametrize(
    "raw",
    [
        True,
        False,
        None,
        0,
        0.0,
        "0",
        "0.0",
        -1,
        -0.5,
        "-1",
        "+1",
        "1e3",
        "1E3",
        "1_0",
        "1,5",
        "NaN",
        "nan",
        "Infinity",
        "inf",
        "-inf",
        float("nan"),
        float("inf"),
        "",
        " ",
        "abc",
        "1.",
        ".5",
        "0x10",
        "١٢",
        [1],
        {"qty": 1},
    ],
)
def test_quantity_forms_that_are_refused(raw):
    assert refusal({"quantity": raw}) == "quantity must be a positive number"


def test_missing_quantity():
    body = payload()
    del body["quantity"]
    with pytest.raises(AlertError) as caught:
        parse_alert(body, settings())
    assert caught.value.message == "quantity must be a positive number"


def test_float_quantity_keeps_its_shortest_repr():
    assert parse({"quantity": 0.1}).quantity == Decimal("0.1")
    assert dec_str(parse({"quantity": 1e-7}).quantity) == "0.0000001"


def test_max_qty_boundary():
    assert parse({"quantity": 100}).quantity == 100
    assert refusal({"quantity": 100.01}) == "quantity exceeds MAX_QTY"
    assert refusal({"quantity": "101"}) == "quantity exceeds MAX_QTY"
    assert parse({"quantity": 5}, MAX_QTY="5").quantity == 5
    assert refusal({"quantity": 6}, MAX_QTY="5") == "quantity exceeds MAX_QTY"
    assert refusal({"quantity": 10**30}) == "quantity exceeds MAX_QTY"


# order type, price, time in force


@pytest.mark.parametrize("raw,expected", [("market", "market"), ("LIMIT", "limit"), (" Market ", "market")])
def test_order_type_normalisation(raw, expected):
    overrides = {"order_type": raw}
    if expected == "limit":
        overrides["limit_price"] = 10
    assert parse(overrides).order_type == expected


@pytest.mark.parametrize("raw", ["stop", "", None, 1, ["market"]])
def test_invalid_order_type(raw):
    assert refusal({"order_type": raw}) == "order_type must be market or limit"


@pytest.mark.parametrize("price", [None, 0, -1, "0", "abc", "1e3", True, "NaN", float("inf"), ""])
def test_limit_order_needs_a_positive_price(price):
    assert refusal({"order_type": "limit", "limit_price": price}) == "limit_price is required for limit orders"


def test_limit_order_without_a_price_field():
    assert refusal({"order_type": "limit"}) == "limit_price is required for limit orders"


@pytest.mark.parametrize("price,expected", [(10, "10"), (187.5, "187.5"), ("187.50", "187.50"), (" 3.2 ", "3.2")])
def test_limit_price_forms(price, expected):
    assert parse({"order_type": "limit", "limit_price": price}).limit_price == Decimal(expected)


def test_limit_price_is_ignored_for_market_orders():
    intent = parse({"limit_price": "not a number"})
    assert intent.order_type == "market" and intent.limit_price is None


@pytest.mark.parametrize("raw,expected", [("day", "day"), ("GTC", "gtc"), (" Day ", "day")])
def test_time_in_force_normalisation(raw, expected):
    assert parse({"time_in_force": raw}).time_in_force == expected


@pytest.mark.parametrize("raw", ["ioc", "fok", "", None, 1])
def test_invalid_time_in_force(raw):
    assert refusal({"time_in_force": raw}) == "time_in_force must be day or gtc"


# order of checks


def test_first_failing_field_wins():
    # broker, symbol, action, quantity, order type, price, time in force are checked in that order
    body = {"broker": "x", "symbol": "!", "action": "hold", "quantity": -1}
    with pytest.raises(AlertError) as caught:
        parse_alert({"secret": SECRET, **body}, settings())
    assert caught.value.message == "unknown broker"
    body["broker"] = "alpaca"
    with pytest.raises(AlertError) as caught:
        parse_alert({"secret": SECRET, **body}, settings())
    assert caught.value.message == "invalid symbol"
    body["symbol"] = "AAPL"
    with pytest.raises(AlertError) as caught:
        parse_alert({"secret": SECRET, **body}, settings())
    assert caught.value.message == "action must be buy or sell"
    body["action"] = "buy"
    with pytest.raises(AlertError) as caught:
        parse_alert({"secret": SECRET, **body}, settings())
    assert caught.value.message == "quantity must be a positive number"
