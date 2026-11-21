"""Logging setup and the filter that keeps credentials out of every log record."""

from __future__ import annotations

import logging
import threading
from typing import Any

REDACTED = "<redacted>"
LOGGER_NAME = "tvbot"
_HANDLER_MARK = "_tvbot_stream_handler"
_QUIET_LOGGERS = ("httpx", "httpcore", "schwab", "authlib")
_MIN_SECRET_LENGTH = 4  # shorter values would mangle unrelated log text


class Scrubber:
    """Replaces every known secret value in a text with <redacted>."""

    def __init__(self, settings: Any) -> None:
        self._lock = threading.Lock()
        self._values: tuple[str, ...] = ()
        for name in (
            "webhook_secret",
            "alpaca_api_key_id",
            "alpaca_api_secret_key",
            "tradier_access_token",
            "tradier_account_id",
            "tastytrade_client_secret",
            "tastytrade_refresh_token",
            "tastytrade_account_number",
            "schwab_app_key",
            "schwab_app_secret",
            "schwab_account_number",
        ):
            self.add(getattr(settings, name, ""))

    def add(self, value: str | None) -> None:
        """Register a secret; later registrations (runtime tokens, hashes) take effect at once."""
        if not value or len(value) < _MIN_SECRET_LENGTH:
            return
        with self._lock:
            if value not in self._values:
                self._values = tuple(sorted((*self._values, value), key=len, reverse=True))

    def scrub(self, text: str) -> str:
        for value in self._values:  # longest first, so a secret containing another is replaced whole
            text = text.replace(value, REDACTED)
        return text


class SecretFilter(logging.Filter):
    """Rewrites each record to its scrubbed text; it never raises into the caller."""

    def __init__(self, scrubber: Scrubber) -> None:
        super().__init__()
        self.scrubber = scrubber

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            if record.stack_info:
                text += "\n" + record.stack_info
            record.msg = self.scrubber.scrub(text)
        except Exception:
            record.msg = "log message dropped: scrub failed"
        record.args = None
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def setup_logging(settings: Any, scrubber: Scrubber) -> None:
    """Configure the `tvbot` logger; safe to call more than once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(settings.log_level)
    for name in _QUIET_LOGGERS:  # httpx logs request URLs, which carry account ids, at INFO
        logging.getLogger(name).setLevel(logging.WARNING)
    for existing in list(logger.filters):
        if isinstance(existing, SecretFilter):
            logger.removeFilter(existing)
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            logger.removeHandler(handler)
    logger.addFilter(SecretFilter(scrubber))
    if not logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        setattr(handler, _HANDLER_MARK, True)
        logger.addHandler(handler)
