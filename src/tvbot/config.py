"""Settings read once from the environment at startup."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

BROKER_NAMES = ("alpaca", "tradier", "tastytrade", "schwab")
DEFAULT_TRADINGVIEW_IPS = (
    "52.89.214.238",
    "34.212.75.30",
    "54.218.53.128",
    "52.32.178.7",
)
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MIN_SECRET_LENGTH = 16
# The value shipped in .env.example; running with it would expose a public secret.
PLACEHOLDER_SECRET = "your-long-random-secret"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_NUMBER = re.compile(r"[0-9]+(\.[0-9]+)?")
_INTEGER = re.compile(r"[0-9]+")


class ConfigError(RuntimeError):
    """Raised at startup when an environment variable is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    webhook_secret: str = field(repr=False)
    dry_run: bool
    default_broker: str
    max_qty: Decimal
    allowed_symbols: tuple[str, ...]
    clamp_sells: bool
    dedupe_seconds: int
    tradingview_ips_only: bool
    tradingview_ips: tuple[str, ...]
    broker_timeout_seconds: float
    log_level: str
    alpaca_api_key_id: str = field(repr=False)
    alpaca_api_secret_key: str = field(repr=False)
    alpaca_live: bool
    tradier_access_token: str = field(repr=False)
    tradier_account_id: str = field(repr=False)
    tradier_live: bool
    tastytrade_client_secret: str = field(repr=False)
    tastytrade_refresh_token: str = field(repr=False)
    tastytrade_account_number: str = field(repr=False)
    tastytrade_live: bool
    schwab_app_key: str = field(repr=False)
    schwab_app_secret: str = field(repr=False)
    schwab_account_number: str = field(repr=False)
    schwab_callback_url: str
    schwab_token_path: str
    schwab_live: bool
    schwab_allow_unchecked_sells: bool


def _text(environ: Mapping[str, str], name: str, default: str = "") -> str:
    value = environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _text(environ, name)
    if not raw:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")


def _positive_decimal(environ: Mapping[str, str], name: str, default: str) -> Decimal:
    raw = _text(environ, name, default)
    if not _NUMBER.fullmatch(raw):
        raise ConfigError(f"{name} must be a positive number")
    value = Decimal(raw)
    if not value.is_finite() or value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    return value


def _positive_float(environ: Mapping[str, str], name: str, default: str) -> float:
    raw = _text(environ, name, default)
    if not _NUMBER.fullmatch(raw):
        raise ConfigError(f"{name} must be a positive number")
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    return value


def _non_negative_int(environ: Mapping[str, str], name: str, default: str) -> int:
    raw = _text(environ, name, default)
    if not _INTEGER.fullmatch(raw):
        raise ConfigError(f"{name} must be a whole number of seconds, 0 or more")
    return int(raw)


def _items(
    environ: Mapping[str, str], name: str, default: tuple[str, ...], upper: bool
) -> tuple[str, ...]:
    raw = environ.get(name)
    if raw is None:
        return default
    parts = (part.strip() for part in raw.split(","))
    return tuple(part.upper() if upper else part for part in parts if part)


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Build Settings from a mapping such as os.environ; raise ConfigError on bad input."""
    secret = environ.get("WEBHOOK_SECRET") or ""
    if len(secret) < MIN_SECRET_LENGTH:
        raise ConfigError("WEBHOOK_SECRET must be set to at least 16 characters")
    if secret == PLACEHOLDER_SECRET:
        raise ConfigError("WEBHOOK_SECRET is still the placeholder from .env.example; generate a real one")

    default_broker = _text(environ, "DEFAULT_BROKER", "alpaca").lower()
    if default_broker not in BROKER_NAMES:
        raise ConfigError("DEFAULT_BROKER must be one of " + ", ".join(BROKER_NAMES))

    log_level = _text(environ, "LOG_LEVEL", "INFO").upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError("LOG_LEVEL must be one of " + ", ".join(LOG_LEVELS))

    ips_only = _bool(environ, "TRADINGVIEW_IPS_ONLY", False)
    ips = _items(environ, "TRADINGVIEW_IPS", DEFAULT_TRADINGVIEW_IPS, upper=False)
    if ips_only and not ips:
        raise ConfigError("TRADINGVIEW_IPS must list at least one address when TRADINGVIEW_IPS_ONLY is true")

    return Settings(
        webhook_secret=secret,
        dry_run=_bool(environ, "DRY_RUN", True),
        default_broker=default_broker,
        max_qty=_positive_decimal(environ, "MAX_QTY", "100"),
        allowed_symbols=_items(environ, "ALLOWED_SYMBOLS", (), upper=True),
        clamp_sells=_bool(environ, "CLAMP_SELLS", False),
        dedupe_seconds=_non_negative_int(environ, "DEDUPE_SECONDS", "30"),
        tradingview_ips_only=ips_only,
        tradingview_ips=ips,
        broker_timeout_seconds=_positive_float(environ, "BROKER_TIMEOUT_SECONDS", "10"),
        log_level=log_level,
        alpaca_api_key_id=_text(environ, "ALPACA_API_KEY_ID"),
        alpaca_api_secret_key=_text(environ, "ALPACA_API_SECRET_KEY"),
        alpaca_live=_bool(environ, "ALPACA_LIVE", False),
        tradier_access_token=_text(environ, "TRADIER_ACCESS_TOKEN"),
        tradier_account_id=_text(environ, "TRADIER_ACCOUNT_ID"),
        tradier_live=_bool(environ, "TRADIER_LIVE", False),
        tastytrade_client_secret=_text(environ, "TASTYTRADE_CLIENT_SECRET"),
        tastytrade_refresh_token=_text(environ, "TASTYTRADE_REFRESH_TOKEN"),
        tastytrade_account_number=_text(environ, "TASTYTRADE_ACCOUNT_NUMBER"),
        tastytrade_live=_bool(environ, "TASTYTRADE_LIVE", False),
        schwab_app_key=_text(environ, "SCHWAB_APP_KEY"),
        schwab_app_secret=_text(environ, "SCHWAB_APP_SECRET"),
        schwab_account_number=_text(environ, "SCHWAB_ACCOUNT_NUMBER"),
        schwab_callback_url=_text(environ, "SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"),
        schwab_token_path=_text(environ, "SCHWAB_TOKEN_PATH", "schwab_token.json"),
        schwab_live=_bool(environ, "SCHWAB_LIVE", False),
        schwab_allow_unchecked_sells=_bool(environ, "SCHWAB_ALLOW_UNCHECKED_SELLS", False),
    )
