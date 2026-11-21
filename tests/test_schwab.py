"""Schwab adapter and the login helper: guards, builders, account lookup, client cache and error mapping."""

from __future__ import annotations

import json
import logging
import os
import runpy
import sys
import threading
import time
from decimal import Decimal

import httpx
import pytest
from conftest import (
    ALL_SECRETS,
    SCHWAB_ACCOUNT,
    SCHWAB_KEY,
    SCHWAB_SECRET,
    CountingFactory,
    FakeSchwabClient,
    Router,
    alert,
    assert_no_secrets,
    build,
    build_live,
    make_settings,
    reply,
)

from tvbot import schwab_login
from tvbot.brokers.base import GuardRefused, NotConfigured, OrderIntent
from tvbot.brokers.schwab import EXTRA_MESSAGE, LOGIN_MESSAGE, SchwabBroker, default_client_factory

LIVE = {"SCHWAB_LIVE": "true"}
UNCHECKED = {"SCHWAB_LIVE": "true", "SCHWAB_ALLOW_UNCHECKED_SELLS": "true"}
PREVIEW_URL = "https://api.schwabapi.com/trader/v1/accounts/<SCHWAB_ACCOUNT_HASH>/orders"
NOT_LIVE = "Schwab orders from this bot are real orders; set SCHWAB_LIVE=true to place them"
SELLS_OFF = (
    "Schwab sells are off: this bot cannot check your Schwab position. "
    "Set SCHWAB_ALLOW_UNCHECKED_SELLS=true to send sells as Schwab SELL orders"
)
NO_ACCOUNT = "SCHWAB_ACCOUNT_NUMBER is not one of the accounts this Schwab login can trade"
SAVED = (
    "Saved the Schwab token to schwab_token.json. Schwab tokens last seven days; "
    "run this command again when orders start failing with an auth error."
)


class FakeOAuthError(Exception):
    """Stands in for an Authlib error: only the defining module matters to the adapter."""


FakeOAuthError.__module__ = "authlib.integrations.base_client.errors"


class DerivedOAuthError(FakeOAuthError):
    """Defined outside Authlib but derived from one of its classes."""


class LookalikeError(Exception):
    pass


LookalikeError.__module__ = "notauthlib.errors"


def post(client, payload):
    return client.post("/webhook", json=payload)


def schwab(**extra):
    return alert(broker="schwab", **extra)


def started(fake=None, env=None, factory=None):
    """A live app with a fake client; returns (TestClient, fake client, factory)."""
    fake = fake or FakeSchwabClient()
    factory = factory or CountingFactory(fake)
    client, _ = build_live(None, {**LIVE, **(env or {})}, schwab_client_factory=factory)
    return client, fake, factory


def created(order_id="12345", account="HASHX", status=201):
    location = f"https://api.schwabapi.com/trader/v1/accounts/{account}/orders/{order_id}"
    return httpx.Response(status, headers={"Location": location})


def accounts_reply(body):
    return httpx.Response(200, content=json.dumps(body).encode(), headers={"content-type": "application/json"})


def warnings_of(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "tvbot" and r.levelno == logging.WARNING]


def built(fake, index=-1):
    return fake.placed[index][1].build()


def leg(build_output):
    [only] = build_output["orderLegCollection"]
    return only


# guards


def test_orders_need_the_live_flag():
    client, fake, _ = started(env={"SCHWAB_LIVE": "false"})
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (422, {"ok": False, "error": NOT_LIVE})
    assert fake.placed == [] and fake.account_calls == 0


def test_sells_need_their_own_flag():
    client, fake, _ = started()
    response = post(client, schwab(action="sell"))
    assert (response.status_code, response.json()) == (422, {"ok": False, "error": SELLS_OFF})
    assert fake.placed == [] and fake.account_calls == 0
    assert post(client, schwab()).status_code == 200  # a buy needs only the live flag


def test_sells_go_out_as_sell_orders_when_allowed():
    client, fake, _ = started(env=UNCHECKED)
    response = post(client, schwab(action="sell", quantity=4))
    assert response.status_code == 200
    assert (leg(built(fake))["instruction"], leg(built(fake))["quantity"]) == ("SELL", 4)


def test_the_live_check_comes_before_the_sell_check():
    client, fake, _ = started(env={"SCHWAB_LIVE": "false"})
    assert post(client, schwab(action="sell")).json()["error"] == NOT_LIVE


def test_the_allow_flag_alone_does_not_make_orders_live():
    client, fake, _ = started(env={"SCHWAB_LIVE": "false", "SCHWAB_ALLOW_UNCHECKED_SELLS": "true"})
    assert post(client, schwab(action="sell")).json()["error"] == NOT_LIVE
    assert fake.placed == []


@pytest.mark.parametrize(
    "env,side,message",
    [({}, "buy", NOT_LIVE), ({}, "sell", NOT_LIVE), (LIVE, "sell", SELLS_OFF)],
)
def test_the_guards_run_before_any_client_is_created(env, side, message):
    factory = CountingFactory(FakeSchwabClient())
    broker = SchwabBroker(make_settings(**env), factory)
    intent = OrderIntent("schwab", "AAPL", side, Decimal(1), "market", None, "day")
    with pytest.raises(GuardRefused) as caught:
        broker.place(intent, False)
    assert str(caught.value) == message
    assert factory.calls == 0


def test_fractional_quantity_is_refused_before_the_live_check():
    client, fake, _ = started(env={"SCHWAB_LIVE": "false"})
    response = post(client, schwab(quantity=0.5))
    assert (response.status_code, response.json()) == (
        422,
        {"ok": False, "error": "Schwab orders must be whole shares in this bot"},
    )
    assert fake.placed == [] and fake.account_calls == 0


def test_a_dry_run_needs_no_flag_and_never_creates_a_client():
    factory = CountingFactory(FakeSchwabClient())
    client, _ = build(schwab_client_factory=factory)
    response = post(client, schwab(action="sell"))
    assert response.status_code == 200 and response.json()["dry_run"] is True
    assert response.json()["request"]["url"] == PREVIEW_URL
    assert factory.calls == 0


# request shape, from the real schwab-py builders

SHAPES = [
    ({"action": "buy"}, "BUY", "MARKET", "DAY", None),
    ({"action": "buy", "order_type": "limit", "limit_price": "187.50"}, "BUY", "LIMIT", "DAY", "187.50"),
    ({"action": "buy", "order_type": "limit", "limit_price": 187.5, "time_in_force": "gtc"}, "BUY", "LIMIT", "GOOD_TILL_CANCEL", "187.5"),
    ({"action": "buy", "time_in_force": "gtc"}, "BUY", "MARKET", "GOOD_TILL_CANCEL", None),
    ({"action": "sell"}, "SELL", "MARKET", "DAY", None),
    ({"action": "sell", "order_type": "limit", "limit_price": "99.95", "time_in_force": "gtc"}, "SELL", "LIMIT", "GOOD_TILL_CANCEL", "99.95"),
    ({"action": "sell", "order_type": "limit", "limit_price": "0.0000001"}, "SELL", "LIMIT", "DAY", "0.0000001"),
]


@pytest.mark.parametrize("extra,instruction,order_type,duration,price", SHAPES)
def test_the_order_handed_to_schwab_py_has_the_documented_fields(extra, instruction, order_type, duration, price):
    client, fake, _ = started(env=UNCHECKED)
    response = post(client, schwab(quantity=3, **extra))
    assert response.status_code == 200
    account_hash, builder = fake.placed[-1]
    output = builder.build()
    assert account_hash == "HASHX"
    assert (output["orderType"], output["duration"], output["orderStrategyType"]) == (order_type, duration, "SINGLE")
    assert output.get("price") == price
    only = leg(output)
    assert (only["instruction"], only["quantity"], only["instrument"]["symbol"]) == (instruction, 3, "AAPL")
    assert only["instrument"]["assetType"] == "EQUITY"


def test_a_market_order_has_no_price_even_when_the_alert_names_one():
    client, fake, _ = started()
    post(client, schwab(limit_price=99))
    assert "price" not in built(fake)


@pytest.mark.parametrize("quantity", [2.0, "2", "2.000"])
def test_the_quantity_is_a_plain_integer(quantity):
    client, fake, _ = started()
    assert post(client, schwab(quantity=quantity)).status_code == 200
    assert leg(built(fake))["quantity"] == 2 and type(leg(built(fake))["quantity"]) is int


def test_a_limit_price_is_a_string_in_the_order():
    client, fake, _ = started()
    post(client, schwab(order_type="limit", limit_price=187.5))
    assert built(fake)["price"] == "187.5" and isinstance(built(fake)["price"], str)


def test_a_successful_order_answers_with_the_id_and_no_client_id():
    client, fake, _ = started()
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (
        200,
        {"ok": True, "dry_run": False, "broker": "schwab", "order_id": "12345", "client_id": None, "quantity": "1"},
    )


@pytest.mark.parametrize("extra,_instruction,_order_type,_duration,_price", SHAPES)
def test_the_preview_equals_the_order_that_is_handed_over(extra, _instruction, _order_type, _duration, _price):
    payload = schwab(quantity=3, **extra)
    preview = post(build()[0], payload).json()["request"]

    client, fake, _ = started(env=UNCHECKED)
    assert post(client, payload).status_code == 200
    assert preview["method"] == "POST"
    assert preview["url"] == PREVIEW_URL
    assert preview["json"] == built(fake)
    assert "HASHX" not in json.dumps(preview)


# account lookup


def test_the_account_hash_is_looked_up_once_and_reused():
    client, fake, factory = started()
    for _ in range(3):
        assert post(client, schwab()).status_code == 200
    assert fake.account_calls == 1
    assert [account for account, _ in fake.placed] == ["HASHX"] * 3
    assert factory.calls == 1  # the startup warm-up; orders reuse the cached client


def test_the_hash_of_the_matching_account_is_used():
    accounts = [
        {"accountNumber": "111111111", "hashValue": "HASHA"},
        {"accountNumber": SCHWAB_ACCOUNT, "hashValue": "HASHB"},
        {"accountNumber": "222222222", "hashValue": "HASHC"},
    ]
    fake = FakeSchwabClient(on_accounts=lambda: accounts_reply(accounts))
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert (response.status_code, response.json()["order_id"]) == (200, "12345")
    assert fake.placed[0][0] == "HASHB"


def test_an_account_number_that_the_login_cannot_trade_is_a_422_and_is_asked_again_next_time():
    other = [{"accountNumber": "111111111", "hashValue": "HASHA"}]
    fake = FakeSchwabClient(on_accounts=lambda: accounts_reply(other))
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (422, {"ok": False, "error": NO_ACCOUNT})
    assert fake.placed == []
    post(client, schwab())
    assert fake.account_calls == 2


@pytest.mark.parametrize(
    "body",
    [[], {"accountNumber": SCHWAB_ACCOUNT, "hashValue": "HASHX"}, "text", None, 5, ["junk", 5, None], [{"hashValue": "HASHX"}]],
)
def test_an_account_list_that_does_not_contain_the_account_is_a_422(body):
    fake = FakeSchwabClient(on_accounts=lambda: accounts_reply(body))
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert (response.status_code, response.json()["error"]) == (422, NO_ACCOUNT)
    assert fake.placed == []


def test_an_account_number_that_arrives_as_a_number_still_matches():
    fake = FakeSchwabClient(on_accounts=lambda: accounts_reply([{"accountNumber": int(SCHWAB_ACCOUNT), "hashValue": "HASHX"}]))
    client, _, _ = started(fake)
    assert post(client, schwab()).status_code == 200


@pytest.mark.parametrize("entry", [{"accountNumber": SCHWAB_ACCOUNT}, {"accountNumber": SCHWAB_ACCOUNT, "hashValue": ""}, {"accountNumber": SCHWAB_ACCOUNT, "hashValue": 5}])
def test_a_matching_account_without_a_usable_hash_is_a_424(entry):
    fake = FakeSchwabClient(on_accounts=lambda: accounts_reply([entry]))
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (
        424,
        {"ok": False, "error": "unexpected account response from Schwab", "broker_status": 200},
    )
    assert fake.placed == []


@pytest.mark.parametrize(
    "answer,status,text",
    [
        (httpx.Response(401, text="Unauthorized"), 401, "Unauthorized"),
        (httpx.Response(403, text="Forbidden"), 403, "Forbidden"),
        (httpx.Response(503, text="Service Unavailable"), 503, "Service Unavailable"),
    ],
)
def test_a_failed_account_lookup_is_a_424_whatever_its_status(answer, status, text):
    fake = FakeSchwabClient(on_accounts=lambda: answer)
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": text, "broker_status": status})
    assert fake.placed == []


def raising(exc):
    def raiser(*_args):
        raise exc

    return raiser


@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x")])
def test_an_http_error_during_the_lookup_is_a_424_by_class_name(exc):
    fake = FakeSchwabClient(on_accounts=raising(exc))
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (
        424,
        {"ok": False, "error": f"Schwab request failed: {type(exc).__name__}", "broker_status": None},
    )
    assert fake.placed == []


@pytest.mark.parametrize("exc", [FakeOAuthError("invalid_client"), DerivedOAuthError("invalid_client")])
def test_an_auth_error_during_the_lookup_is_a_424_and_drops_the_client(exc):
    broken = FakeSchwabClient(on_accounts=raising(exc))
    fresh = FakeSchwabClient()
    factory = CountingFactory(broken, fresh)
    client, _, _ = started(broken, factory=factory)
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": LOGIN_MESSAGE, "broker_status": None})
    assert post(client, schwab()).status_code == 200  # a new client is created for the next order
    assert factory.calls == 2 and fresh.placed


def test_any_other_exception_during_the_lookup_is_a_424_that_names_only_the_class():
    fake = FakeSchwabClient(on_accounts=raising(RuntimeError("detail that must not appear")))
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (
        424,
        {"ok": False, "error": "schwab could not prepare the order: RuntimeError", "broker_status": None},
    )


def test_an_account_answer_that_is_not_json_is_a_424():
    fake = FakeSchwabClient(on_accounts=lambda: httpx.Response(200, text="not json"))
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert (response.status_code, response.json()["error"]) == (424, "schwab could not prepare the order: JSONDecodeError")
    assert fake.placed == []


def test_the_account_hash_never_reaches_the_response_or_the_logs(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeSchwabClient(on_place=lambda account, builder: httpx.Response(400, text=f"no such account {account}"))
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert (response.status_code, response.json()["error"]) == (424, "no such account <redacted>")
    assert_no_secrets(caplog, response.text, secrets=[*ALL_SECRETS, "HASHX"])


# sending


@pytest.mark.parametrize(
    "answer,expected",
    [
        (created("12345"), "12345"),
        (created("777", status=200), "777"),
        (created("12345", account="OTHERHASH"), None),
        (httpx.Response(201), None),
        (httpx.Response(201, headers={"Location": "https://example.invalid/somewhere"}), None),
        (httpx.Response(201, headers={"Location": "https://api.schwabapi.com/trader/v1/accounts/HASHX/orders/notanumber"}), None),
        (httpx.Response(204), None),
    ],
)
def test_the_order_id_comes_from_the_location_header_and_only_when_it_matches(answer, expected):
    fake = FakeSchwabClient(on_place=lambda account, builder: answer)
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert response.status_code == 200
    assert response.json()["order_id"] == expected


def test_an_order_whose_id_cannot_be_read_is_still_a_success(monkeypatch):
    monkeypatch.setitem(sys.modules, "schwab.utils", None)
    client, fake, _ = started()
    response = post(client, schwab())
    assert (response.status_code, response.json()["order_id"]) == (200, None)
    assert len(fake.placed) == 1


@pytest.mark.parametrize(
    "answer,status,text",
    [
        (httpx.Response(400, text="Bad request body"), 400, "Bad request body"),
        (httpx.Response(401, text="Unauthorized"), 401, "Unauthorized"),
        (httpx.Response(403, text="Forbidden"), 403, "Forbidden"),
        (httpx.Response(404, text=""), 404, ""),
        (httpx.Response(429, text="Too many requests"), 429, "Too many requests"),
        (httpx.Response(302, text="moved"), 302, "moved"),
    ],
)
def test_a_refusal_is_a_424_that_carries_the_status_and_text(answer, status, text):
    fake = FakeSchwabClient(on_place=lambda account, builder: answer)
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": text, "broker_status": status})


def test_a_json_refusal_is_passed_through_as_its_text():
    body = {"message": "Validation failed", "errors": ["quantity"]}
    fake = FakeSchwabClient(on_place=lambda account, builder: httpx.Response(400, json=body))
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert json.loads(response.json()["error"]) == body


@pytest.mark.parametrize("status", [500, 502, 503, 599])
def test_a_5xx_is_a_504_that_points_at_the_schwab_orders_and_is_not_resent(status):
    fake = FakeSchwabClient(on_place=lambda account, builder: httpx.Response(status, text="upstream"))
    client, _, _ = started(fake, env={"DEDUPE_SECONDS": "30"})
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (
        504,
        {"ok": False, "error": f"outcome unknown (Schwab returned HTTP {status}); the order may have been placed. Check your Schwab orders."},
    )
    assert post(client, schwab()).json() == {"ok": True, "duplicate": True}
    assert len(fake.placed) == 1


@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.PoolTimeout("x")])
def test_a_failure_before_the_request_left_is_a_424(exc):
    fake = FakeSchwabClient(on_place=raising(exc))
    client, _, _ = started(fake, env={"DEDUPE_SECONDS": "30"})
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (
        424,
        {"ok": False, "error": f"could not reach schwab: {type(exc).__name__}", "broker_status": None},
    )
    fake.on_place = None
    assert post(client, schwab()).status_code == 200  # a refused attempt can be retried at once


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("x"),
        httpx.WriteTimeout("x"),
        httpx.ReadError("x"),
        httpx.RemoteProtocolError("x"),
        TimeoutError("x"),
        OSError("x"),
        RuntimeError("detail that must not appear"),
        LookalikeError("not from authlib"),
    ],
)
def test_any_other_failure_after_the_call_started_is_a_504_by_class_name(exc):
    fake = FakeSchwabClient(on_place=raising(exc))
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (
        504,
        {"ok": False, "error": f"outcome unknown ({type(exc).__name__}); the order may have been placed. Check your Schwab orders."},
    )


def real_oauth_error():
    from authlib.integrations.base_client.errors import OAuthError

    return OAuthError(error="invalid_client", description="refresh token too old")


@pytest.mark.parametrize("make", [lambda: FakeOAuthError("x"), lambda: DerivedOAuthError("x"), real_oauth_error])
def test_an_expired_login_is_a_424_that_says_how_to_log_in_and_drops_the_client(make):
    first = FakeSchwabClient(on_place=raising(make()))
    second = FakeSchwabClient()
    factory = CountingFactory(first, second)
    client, _, _ = started(first, factory=factory)
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (424, {"ok": False, "error": LOGIN_MESSAGE, "broker_status": None})
    assert "refresh token too old" not in response.text

    assert post(client, schwab()).status_code == 200
    assert factory.calls == 2  # one at startup, one after the expired login
    assert len(second.placed) == 1 and second.account_calls == 1  # the hash was looked up again too


def test_an_exception_message_never_reaches_the_response_or_the_logs(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeSchwabClient(on_place=raising(RuntimeError("token abc123 for HASHX")))
    client, _, _ = started(fake)
    response = post(client, schwab())
    assert response.status_code == 504
    assert_no_secrets(caplog, response.text, secrets=[*ALL_SECRETS, "HASHX"])
    assert "abc123" not in response.text


def test_a_missing_extra_is_a_422_with_the_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "schwab.orders.common", None)
    client, fake, _ = started()
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (422, {"ok": False, "error": EXTRA_MESSAGE})
    assert fake.placed == []


def test_schwab_calls_never_overlap_even_for_different_symbols():
    gate = threading.Event()
    entered = threading.Event()
    state = {"active": 0, "max": 0}
    mutex = threading.Lock()

    def slow_place(account, builder):
        with mutex:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        entered.set()
        gate.wait(timeout=10)
        with mutex:
            state["active"] -= 1
        return created()

    fake = FakeSchwabClient(on_place=slow_place)
    broker = SchwabBroker(make_settings(SCHWAB_LIVE="true"), CountingFactory(fake))
    results = []
    threads = [
        threading.Thread(
            target=lambda symbol=symbol: results.append(
                broker.place(OrderIntent("schwab", symbol, "buy", Decimal(1), "market", None, "day"), False)
            )
        )
        for symbol in ("AAPL", "MSFT")
    ]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(timeout=10)
        time.sleep(0.3)
        assert len(fake.placed) == 1  # the second call waits for the first
    finally:
        gate.set()
        for thread in threads:
            thread.join(timeout=10)
    assert len(results) == 2 and state["max"] == 1


# the client cache and the token file


def test_a_newer_token_file_makes_the_next_order_use_a_new_client_and_hash(tmp_path):
    token = tmp_path / "schwab_token.json"
    token.write_text("{}")
    first, second = FakeSchwabClient(), FakeSchwabClient()
    factory = CountingFactory(first, second)
    client, _, _ = started(first, env={"SCHWAB_TOKEN_PATH": str(token)}, factory=factory)
    assert post(client, schwab()).status_code == 200
    assert post(client, schwab()).status_code == 200
    assert (factory.calls, len(first.placed), len(second.placed)) == (1, 2, 0)

    stat = token.stat()
    os.utime(token, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert post(client, schwab()).status_code == 200
    assert (factory.calls, len(first.placed), len(second.placed)) == (2, 2, 1)
    assert (first.account_calls, second.account_calls) == (1, 1)

    assert post(client, schwab()).status_code == 200
    assert (factory.calls, len(second.placed), second.account_calls) == (2, 2, 1)


def test_an_unchanged_token_file_keeps_the_client(tmp_path):
    token = tmp_path / "schwab_token.json"
    token.write_text("{}")
    client, fake, factory = started(env={"SCHWAB_TOKEN_PATH": str(token)})
    for _ in range(3):
        post(client, schwab())
    assert (factory.calls, fake.account_calls, len(fake.placed)) == (1, 1, 3)


def test_a_client_that_could_not_be_created_at_startup_is_created_by_the_first_order():
    fake = FakeSchwabClient()
    calls = []

    def flaky(settings):
        calls.append(1)
        if len(calls) == 1:
            raise NotConfigured("run python -m tvbot.schwab_login first; SCHWAB_TOKEN_PATH was not found")
        return fake

    client, _ = build_live(None, LIVE, schwab_client_factory=flaky)
    assert len(calls) == 1
    assert post(client, schwab()).status_code == 200
    assert (len(calls), len(fake.placed)) == (2, 1)


def test_a_factory_that_fails_is_tried_again_by_every_order_until_it_works():
    fake = FakeSchwabClient()
    calls = []

    def flaky(settings):
        calls.append(1)
        if len(calls) <= 2:
            raise NotConfigured("run python -m tvbot.schwab_login first; SCHWAB_TOKEN_PATH was not found")
        return fake

    client, _ = build_live(None, LIVE, schwab_client_factory=flaky)
    first = post(client, schwab())
    assert (first.status_code, first.json()) == (
        422,
        {"ok": False, "error": "run python -m tvbot.schwab_login first; SCHWAB_TOKEN_PATH was not found"},
    )
    assert fake.placed == []
    assert post(client, schwab()).status_code == 200
    assert (len(calls), len(fake.placed)) == (3, 1)


def test_a_factory_that_never_works_is_a_422_with_its_message():
    client, _ = build_live(None, LIVE, schwab_client_factory=CountingFactory())
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (422, {"ok": False, "error": "no schwab client in this test"})


def test_a_factory_that_raises_something_else_is_a_424_by_class_name():
    def broken(settings):
        raise RuntimeError("detail that must not appear")

    client, _ = build_live(None, LIVE, schwab_client_factory=broken)
    response = post(client, schwab())
    assert (response.status_code, response.json()) == (
        424,
        {"ok": False, "error": "schwab could not prepare the order: RuntimeError", "broker_status": None},
    )


# token age


@pytest.mark.parametrize(
    "age,expected",
    [(518400, None), (518401, 6), (561600, 6), (7 * 86400, 7), (9 * 86400, 9)],
)
def test_an_old_token_is_reported_after_each_order(caplog, age, expected):
    caplog.set_level(logging.DEBUG)
    client, fake, _ = started(FakeSchwabClient(token_age=age))
    before = [m for m in warnings_of(caplog) if "days old" in m]  # the startup check
    assert post(client, schwab()).status_code == 200
    after = [m for m in warnings_of(caplog) if "days old" in m]
    if expected is None:
        assert before == [] and after == []
    else:
        message = f"Schwab token is {expected} days old; run python -m tvbot.schwab_login before it expires at 7 days"
        assert before == [message] and after == [message, message]


def test_a_fresh_token_is_not_reported(caplog):
    caplog.set_level(logging.DEBUG)
    client, fake, _ = started(FakeSchwabClient(token_age=3 * 86400))
    post(client, schwab())
    assert not [m for m in warnings_of(caplog) if "days old" in m]


@pytest.mark.parametrize("age", [RuntimeError("no token"), "old", None])
def test_a_token_age_that_cannot_be_read_never_breaks_an_order(caplog, age):
    caplog.set_level(logging.DEBUG)
    client, fake, _ = started(FakeSchwabClient(token_age=age))
    response = post(client, schwab())
    assert (response.status_code, response.json()["order_id"]) == (200, "12345")
    assert not [m for m in warnings_of(caplog) if "days old" in m]


def test_the_token_age_is_also_reported_when_the_order_fails(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeSchwabClient(token_age=8 * 86400, on_place=lambda account, builder: httpx.Response(400, text="no"))
    client, _, _ = started(fake)
    assert post(client, schwab()).status_code == 424
    assert len([m for m in warnings_of(caplog) if "8 days old" in m]) == 2


# startup


def test_the_client_is_created_once_at_startup_when_live_and_configured():
    fake = FakeSchwabClient()
    factory = CountingFactory(fake)
    build_live(None, LIVE, schwab_client_factory=factory)
    assert factory.calls == 1
    assert (fake.account_calls, fake.placed) == (0, [])


def test_there_is_no_startup_client_in_a_dry_run():
    factory = CountingFactory(FakeSchwabClient())
    build(schwab_client_factory=factory)
    assert factory.calls == 0


@pytest.mark.parametrize("name", ["SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_ACCOUNT_NUMBER"])
def test_there_is_no_startup_client_while_a_schwab_variable_is_missing(name):
    factory = CountingFactory(FakeSchwabClient())
    build_live(None, {**LIVE, name: ""}, schwab_client_factory=factory)
    assert factory.calls == 0


def test_a_startup_failure_is_logged_and_does_not_stop_the_app(caplog):
    caplog.set_level(logging.DEBUG)
    client, _ = build_live(None, LIVE, schwab_client_factory=CountingFactory())
    assert "Schwab client not ready at startup: no schwab client in this test" in warnings_of(caplog)
    assert client.get("/health").status_code == 200


def test_an_unexpected_startup_failure_logs_only_the_class_name(caplog):
    caplog.set_level(logging.DEBUG)

    def broken(settings):
        raise RuntimeError("detail that must not appear")

    client, _ = build_live(None, LIVE, schwab_client_factory=broken)
    assert "Schwab client not ready at startup: RuntimeError" in warnings_of(caplog)
    assert "detail that must not appear" not in caplog.text
    assert client.get("/health").status_code == 200


def test_the_token_age_is_checked_at_startup(caplog):
    caplog.set_level(logging.DEBUG)
    started(FakeSchwabClient(token_age=6.5 * 86400))
    assert "Schwab token is 6 days old; run python -m tvbot.schwab_login before it expires at 7 days" in warnings_of(caplog)


def test_other_brokers_still_work_when_the_schwab_client_is_not_ready():
    router = Router().on("POST", "/v2/orders", reply(200, json={"id": "alpaca-order-1"}))
    client, _ = build_live(router, LIVE, schwab_client_factory=CountingFactory())
    assert post(client, alert()).json()["order_id"] == "alpaca-order-1"
    assert post(client, schwab()).status_code == 422  # not ready, and said so


# the default client factory


def test_the_default_factory_needs_the_token_file(tmp_path):
    settings = make_settings(SCHWAB_TOKEN_PATH=str(tmp_path / "missing.json"))
    with pytest.raises(NotConfigured) as caught:
        default_client_factory(settings)
    assert str(caught.value) == "run python -m tvbot.schwab_login first; SCHWAB_TOKEN_PATH was not found"


def test_the_default_factory_reports_a_missing_extra(tmp_path, monkeypatch):
    token = tmp_path / "schwab_token.json"
    token.write_text("{}")
    monkeypatch.setitem(sys.modules, "schwab.auth", None)
    with pytest.raises(NotConfigured) as caught:
        default_client_factory(make_settings(SCHWAB_TOKEN_PATH=str(token)))
    assert str(caught.value) == EXTRA_MESSAGE


def test_the_default_factory_opens_the_token_file_with_the_app_credentials(tmp_path, monkeypatch):
    token = tmp_path / "schwab_token.json"
    token.write_text("{}")
    calls = []
    marker = object()

    def fake_open(*args, **kwargs):
        calls.append((args, kwargs))
        return marker

    monkeypatch.setattr("schwab.auth.client_from_token_file", fake_open)
    assert default_client_factory(make_settings(SCHWAB_TOKEN_PATH=str(token))) is marker
    assert calls == [((str(token), SCHWAB_KEY, SCHWAB_SECRET), {})]


def test_the_broker_uses_the_default_factory_when_none_is_given():
    broker = SchwabBroker(make_settings(SCHWAB_LIVE="true", SCHWAB_TOKEN_PATH="no-such-token-file.json"))
    intent = OrderIntent("schwab", "AAPL", "buy", Decimal(1), "market", None, "day")
    with pytest.raises(NotConfigured) as caught:
        broker.place(intent, False)
    assert "SCHWAB_TOKEN_PATH was not found" in str(caught.value)


# python -m tvbot.schwab_login


@pytest.fixture
def login(monkeypatch, tmp_path):
    """A clean environment for main(): temp working directory, no .env loading, no Schwab variables."""
    monkeypatch.chdir(tmp_path)
    loaded = []
    monkeypatch.setattr(schwab_login, "load_dotenv", lambda *args, **kwargs: loaded.append((args, kwargs)))
    for name in ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL", "SCHWAB_TOKEN_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SCHWAB_APP_KEY", SCHWAB_KEY)
    monkeypatch.setenv("SCHWAB_APP_SECRET", SCHWAB_SECRET)
    return loaded


def fake_flow(monkeypatch, client=None, error=None):
    calls = []
    two_accounts = FakeSchwabClient(
        on_accounts=lambda: accounts_reply(
            [{"accountNumber": "123456789", "hashValue": "HASHONE"}, {"accountNumber": SCHWAB_ACCOUNT, "hashValue": "HASHTWO"}]
        )
    )

    def flow(*args, **kwargs):
        calls.append((args, kwargs))
        if error is not None:
            raise error
        return client or two_accounts

    monkeypatch.setattr("schwab.auth.client_from_manual_flow", flow)
    return calls


def test_login_saves_the_token_and_lists_masked_accounts(login, monkeypatch, capsys):
    calls = fake_flow(monkeypatch)
    assert schwab_login.main() == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [SAVED, "...789", "...321"]
    for hidden in (SCHWAB_KEY, SCHWAB_SECRET, "123456789", SCHWAB_ACCOUNT, "HASHONE", "HASHTWO"):
        assert hidden not in out
    assert calls == [((SCHWAB_KEY, SCHWAB_SECRET, "https://127.0.0.1:8182", "schwab_token.json"), {})]


def test_login_reads_the_dotenv_file_in_the_working_directory(login, monkeypatch):
    fake_flow(monkeypatch)
    schwab_login.main()
    assert login == [((".env",), {})]


def test_login_passes_the_callback_url_and_token_path_from_the_environment(login, monkeypatch, capsys):
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:9999")
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", "elsewhere/token.json")
    calls = fake_flow(monkeypatch)
    assert schwab_login.main() == 0
    assert calls == [((SCHWAB_KEY, SCHWAB_SECRET, "https://127.0.0.1:9999", "elsewhere/token.json"), {})]
    assert capsys.readouterr().out.startswith("Saved the Schwab token to elsewhere/token.json. ")


def test_login_trims_values_and_treats_blank_optionals_as_unset(login, monkeypatch):
    monkeypatch.setenv("SCHWAB_APP_KEY", f"  {SCHWAB_KEY}  ")
    monkeypatch.setenv("SCHWAB_APP_SECRET", f"\t{SCHWAB_SECRET}\n")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "   ")
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", "")
    calls = fake_flow(monkeypatch)
    assert schwab_login.main() == 0
    assert calls == [((SCHWAB_KEY, SCHWAB_SECRET, "https://127.0.0.1:8182", "schwab_token.json"), {})]


@pytest.mark.parametrize(
    "missing,expected",
    [
        (("SCHWAB_APP_KEY",), "missing SCHWAB_APP_KEY"),
        (("SCHWAB_APP_SECRET",), "missing SCHWAB_APP_SECRET"),
        (("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET"), "missing SCHWAB_APP_KEY"),
    ],
)
def test_login_exits_2_and_names_a_missing_setting(login, monkeypatch, capsys, missing, expected):
    for name in missing:
        monkeypatch.delenv(name)
    calls = fake_flow(monkeypatch)
    assert schwab_login.main() == 2
    assert capsys.readouterr().out.strip() == expected
    assert calls == [] and login == [((".env",), {})]


@pytest.mark.parametrize("blank", ["", "   "])
def test_login_treats_a_blank_key_as_missing(login, monkeypatch, capsys, blank):
    monkeypatch.setenv("SCHWAB_APP_SECRET", blank)
    calls = fake_flow(monkeypatch)
    assert schwab_login.main() == 2
    assert capsys.readouterr().out.strip() == "missing SCHWAB_APP_SECRET"
    assert calls == []


def test_login_exits_2_with_an_install_hint_when_the_extra_is_missing(login, monkeypatch, capsys):
    calls = fake_flow(monkeypatch)
    monkeypatch.setitem(sys.modules, "schwab.auth", None)
    assert schwab_login.main() == 2
    assert capsys.readouterr().out.strip() == EXTRA_MESSAGE
    assert calls == []


@pytest.mark.parametrize("error", [RuntimeError("secret detail"), KeyboardInterrupt(), FakeOAuthError("denied")])
def test_login_exits_1_when_the_flow_fails_and_prints_only_the_class(login, monkeypatch, capsys, error):
    fake_flow(monkeypatch, error=error)
    assert schwab_login.main() == 1
    out = capsys.readouterr().out
    assert out == f"Schwab login failed: {type(error).__name__}\n"
    assert "secret detail" not in out and "denied" not in out
    assert "Saved" not in out


@pytest.mark.parametrize("status", [401, 403, 500])
def test_login_exits_1_when_the_account_lookup_fails(login, monkeypatch, capsys, status):
    client = FakeSchwabClient(on_accounts=lambda: httpx.Response(status, text="body that must not appear"))
    fake_flow(monkeypatch, client=client)
    assert schwab_login.main() == 1
    out = capsys.readouterr().out
    assert out == f"Schwab login failed: account lookup returned HTTP {status}\n"


@pytest.mark.parametrize(
    "answer,name",
    [
        (lambda: httpx.Response(200, text="not json"), "JSONDecodeError"),
        (lambda: httpx.Response(200, json=["junk"]), "AttributeError"),
        (raising(httpx.ReadTimeout("x")), "ReadTimeout"),
        (raising(KeyboardInterrupt()), "KeyboardInterrupt"),
    ],
)
def test_login_exits_1_when_the_account_list_cannot_be_read(login, monkeypatch, capsys, answer, name):
    fake_flow(monkeypatch, client=FakeSchwabClient(on_accounts=answer))
    assert schwab_login.main() == 1
    assert capsys.readouterr().out == f"Schwab login failed: {name}\n"


def test_login_with_no_accounts_still_reports_the_saved_token(login, monkeypatch, capsys):
    fake_flow(monkeypatch, client=FakeSchwabClient(on_accounts=lambda: accounts_reply([])))
    assert schwab_login.main() == 0
    assert capsys.readouterr().out.splitlines() == [SAVED]


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_running_the_module_exits_with_the_code_of_main(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    for name in ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("tvbot.schwab_login", run_name="__main__")
    assert caught.value.code == 2
    assert capsys.readouterr().out.strip() == "missing SCHWAB_APP_KEY"
