"""Secret scrubbing and the logging setup."""

from __future__ import annotations

import io
import logging
import sys
from types import SimpleNamespace

import pytest
from conftest import ALL_SECRETS, WEBHOOK_SECRET, make_settings

from tvbot.logs import REDACTED, Scrubber, SecretFilter, setup_logging


def make_record(msg="hello", args=(), exc_info=None, stack_info=None) -> logging.LogRecord:
    return logging.LogRecord("tvbot", logging.INFO, __file__, 1, msg, args, exc_info, sinfo=stack_info)


# Scrubber


def test_scrubber_replaces_every_configured_secret():
    scrubber = Scrubber(make_settings())
    cleaned = scrubber.scrub(" | ".join(ALL_SECRETS))
    for secret in ALL_SECRETS:
        assert secret not in cleaned
    assert cleaned.count(REDACTED) == len(ALL_SECRETS)


def test_scrubber_replaces_a_secret_that_contains_another_whole():
    scrubber = Scrubber(SimpleNamespace(webhook_secret="abcd", alpaca_api_key_id="abcd-longer"))
    assert scrubber.scrub("abcd-longer and abcd") == f"{REDACTED} and {REDACTED}"


def test_scrubber_takes_runtime_secrets_at_once():
    scrubber = Scrubber(make_settings())
    text = "token runtime-value-777 seen"
    assert scrubber.scrub(text) == text
    scrubber.add("runtime-value-777")
    assert scrubber.scrub(text) == f"token {REDACTED} seen"
    scrubber.add("runtime-value-777")  # adding twice changes nothing
    assert scrubber.scrub(text) == f"token {REDACTED} seen"


@pytest.mark.parametrize("value", ["a", "12", "abc"])
def test_scrubber_ignores_values_shorter_than_four_characters(value):
    text = "a 12 abc banana 123 and abc"
    assert Scrubber(SimpleNamespace(webhook_secret=value)).scrub(text) == text
    scrubber = Scrubber(SimpleNamespace())
    scrubber.add(value)
    assert scrubber.scrub(text) == text


def test_scrubber_keeps_a_value_of_exactly_four_characters():
    scrubber = Scrubber(SimpleNamespace(webhook_secret="abcd"))
    assert scrubber.scrub("x abcd y") == f"x {REDACTED} y"


@pytest.mark.parametrize("value", ["", None])
def test_scrubber_ignores_empty_values(value):
    scrubber = Scrubber(SimpleNamespace())
    scrubber.add(value)
    assert scrubber.scrub("nothing to hide here") == "nothing to hide here"


def test_scrubber_with_unset_settings_changes_nothing():
    scrubber = Scrubber(make_settings(ALPACA_API_KEY_ID="", TRADIER_ACCOUNT_ID=""))
    assert scrubber.scrub("plain text") == "plain text"


# SecretFilter


def test_filter_scrubs_message_and_arguments():
    flt = SecretFilter(Scrubber(make_settings()))
    record = make_record("token %s is 100%% wrong", (WEBHOOK_SECRET,))
    assert flt.filter(record) is True
    assert record.getMessage() == f"token {REDACTED} is 100% wrong"
    assert record.args is None


def test_filter_keeps_percent_signs_in_scrubbed_text_literal():
    flt = SecretFilter(Scrubber(make_settings()))
    record = make_record(f"{WEBHOOK_SECRET} 50% %s %d")
    assert flt.filter(record) is True
    assert record.getMessage() == f"{REDACTED} 50% %s %d"


def test_filter_scrubs_the_traceback_text():
    flt = SecretFilter(Scrubber(make_settings()))
    try:
        raise RuntimeError(f"boom {WEBHOOK_SECRET} {ALL_SECRETS[3]}")
    except RuntimeError:
        record = make_record("failed", exc_info=sys.exc_info())
    assert flt.filter(record) is True
    message = record.getMessage()
    assert "Traceback" in message and "RuntimeError" in message
    assert WEBHOOK_SECRET not in message and ALL_SECRETS[3] not in message
    assert REDACTED in message
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None


def test_filter_scrubs_stack_info():
    flt = SecretFilter(Scrubber(make_settings()))
    record = make_record("where", stack_info=f"stack with {WEBHOOK_SECRET}")
    flt.filter(record)
    assert WEBHOOK_SECRET not in record.getMessage()
    assert "stack with" in record.getMessage()


def test_filter_that_fails_still_returns_true_and_drops_the_text():
    scrubber = Scrubber(make_settings())

    def broken(_text):
        raise RuntimeError("scrubber broke")

    scrubber.scrub = broken  # type: ignore[method-assign]
    flt = SecretFilter(scrubber)
    try:
        raise ValueError(WEBHOOK_SECRET)
    except ValueError:
        record = make_record(f"carries {WEBHOOK_SECRET}", exc_info=sys.exc_info(), stack_info="s")
    assert flt.filter(record) is True
    assert record.getMessage() == "log message dropped: scrub failed"
    assert record.args is None and record.exc_info is None and record.exc_text is None and record.stack_info is None


def test_filter_survives_a_record_whose_arguments_cannot_be_formatted():
    flt = SecretFilter(Scrubber(make_settings()))
    record = make_record("%d items", ("not a number",))
    assert flt.filter(record) is True
    assert record.getMessage() == "log message dropped: scrub failed"


def test_filter_on_a_logger_scrubs_before_any_handler_sees_the_record():
    logger = logging.getLogger("tvbot-test-isolated")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    logger.addFilter(SecretFilter(Scrubber(make_settings())))
    try:
        try:
            raise RuntimeError(WEBHOOK_SECRET)
        except RuntimeError:
            logger.exception("order failed for %s", ALL_SECRETS[2])
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert WEBHOOK_SECRET not in output and ALL_SECRETS[2] not in output
    assert output.count(REDACTED) >= 2


# setup_logging


@pytest.fixture
def tvbot_logger():
    logger = logging.getLogger("tvbot")
    handlers_before = list(logger.handlers)
    yield logger
    for added in [h for h in logger.handlers if h not in handlers_before]:
        logger.removeHandler(added)


def test_setup_sets_the_level_and_quiets_the_http_libraries(tvbot_logger):
    for name in ("httpx", "httpcore", "schwab", "authlib"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    settings = make_settings(LOG_LEVEL="WARNING")
    setup_logging(settings, Scrubber(settings))
    assert tvbot_logger.level == logging.WARNING
    for name in ("httpx", "httpcore", "schwab", "authlib"):
        assert logging.getLogger(name).level == logging.WARNING
    assert tvbot_logger.propagate is True


def test_setup_is_repeatable_and_keeps_one_filter(tvbot_logger):
    settings = make_settings()
    first, second = Scrubber(settings), Scrubber(settings)
    setup_logging(settings, first)
    setup_logging(settings, second)
    filters = [f for f in tvbot_logger.filters if isinstance(f, SecretFilter)]
    assert len(filters) == 1
    assert filters[0].scrubber is second


def test_records_reach_root_handlers_already_scrubbed(tvbot_logger, caplog):
    settings = make_settings()
    setup_logging(settings, Scrubber(settings))
    with caplog.at_level(logging.DEBUG):
        tvbot_logger.warning("credential %s used", WEBHOOK_SECRET)
    assert WEBHOOK_SECRET not in caplog.text
    assert f"credential {REDACTED} used" in caplog.text


def test_a_stream_handler_is_added_only_when_nothing_else_handles_records(tvbot_logger, monkeypatch):
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    settings = make_settings()
    before = list(tvbot_logger.handlers)
    setup_logging(settings, Scrubber(settings))
    added = [h for h in tvbot_logger.handlers if h not in before]
    assert len(added) == 1 and isinstance(added[0], logging.StreamHandler)
    assert added[0].formatter._fmt == "%(asctime)s %(levelname)s %(message)s"
    setup_logging(settings, Scrubber(settings))  # a second call replaces it instead of stacking
    assert len([h for h in tvbot_logger.handlers if h not in before]) == 1

    stream = io.StringIO()
    [handler] = [h for h in tvbot_logger.handlers if h not in before]
    handler.stream = stream
    tvbot_logger.warning("token %s", WEBHOOK_SECRET)
    assert f"WARNING token {REDACTED}" in stream.getvalue()
    assert WEBHOOK_SECRET not in stream.getvalue()


def test_no_stream_handler_when_the_root_logger_has_one(tvbot_logger):
    assert logging.getLogger().handlers  # pytest's capture handler
    before = list(tvbot_logger.handlers)
    settings = make_settings()
    setup_logging(settings, Scrubber(settings))
    assert tvbot_logger.handlers == before


def test_no_stream_handler_when_the_application_logger_has_one(tvbot_logger, monkeypatch):
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    custom = logging.NullHandler()
    tvbot_logger.addHandler(custom)
    before = list(tvbot_logger.handlers)
    settings = make_settings()
    setup_logging(settings, Scrubber(settings))
    assert tvbot_logger.handlers == before
