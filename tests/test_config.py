"""Settings parsing: defaults, booleans, numbers, lists and the startup refusals."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest

from tvbot.config import DEFAULT_TRADINGVIEW_IPS, ConfigError, Settings, load_settings

SECRET = "x" * 32
ROOT = Path(__file__).resolve().parents[1]

BOOLEAN_NAMES = [
    "DRY_RUN",
    "CLAMP_SELLS",
    "TRADINGVIEW_IPS_ONLY",
    "ALPACA_LIVE",
    "TRADIER_LIVE",
    "TASTYTRADE_LIVE",
    "SCHWAB_LIVE",
    "SCHWAB_ALLOW_UNCHECKED_SELLS",
]


def load(**env: str) -> Settings:
    return load_settings({"WEBHOOK_SECRET": SECRET, **env})


def test_defaults():
    s = load()
    assert s.dry_run is True
    assert s.default_broker == "alpaca"
    assert s.max_qty == Decimal("100")
    assert s.allowed_symbols == ()
    assert s.clamp_sells is False
    assert s.dedupe_seconds == 30
    assert s.tradingview_ips_only is False
    assert s.tradingview_ips == DEFAULT_TRADINGVIEW_IPS
    assert s.broker_timeout_seconds == 10.0
    assert s.log_level == "INFO"
    assert s.alpaca_live is s.tradier_live is s.tastytrade_live is s.schwab_live is False
    assert s.schwab_allow_unchecked_sells is False
    assert s.schwab_callback_url == "https://127.0.0.1:8182"
    assert s.schwab_token_path == "schwab_token.json"
    assert s.alpaca_api_key_id == s.tradier_account_id == s.schwab_account_number == ""


def test_default_tradingview_ips_are_the_four_published_addresses():
    assert DEFAULT_TRADINGVIEW_IPS == ("52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7")


@pytest.mark.parametrize("env", [{}, {"WEBHOOK_SECRET": ""}, {"WEBHOOK_SECRET": "y" * 15}])
def test_webhook_secret_missing_or_short_is_refused(env):
    with pytest.raises(ConfigError, match="WEBHOOK_SECRET must be set to at least 16 characters"):
        load_settings(env)


def test_webhook_secret_of_exactly_16_characters_is_accepted():
    assert load_settings({"WEBHOOK_SECRET": "z" * 16}).webhook_secret == "z" * 16


def test_the_placeholder_from_env_example_is_refused():
    with pytest.raises(ConfigError, match="placeholder"):
        load_settings({"WEBHOOK_SECRET": "your-long-random-secret"})


def test_config_error_is_a_runtime_error():
    assert issubclass(ConfigError, RuntimeError)


@pytest.mark.parametrize("raw", ["true", "TRUE", " True ", "1", "yes", "YES", "on", "On"])
def test_true_spellings(raw):
    assert load(DRY_RUN="false", CLAMP_SELLS=raw).clamp_sells is True
    assert load(DRY_RUN=raw).dry_run is True


@pytest.mark.parametrize("raw", ["false", "FALSE", " False ", "0", "no", "NO", "off", "Off"])
def test_false_spellings(raw):
    assert load(DRY_RUN=raw).dry_run is False
    assert load(CLAMP_SELLS=raw).clamp_sells is False


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_boolean_means_the_default(raw):
    assert load(DRY_RUN=raw).dry_run is True
    assert load(CLAMP_SELLS=raw).clamp_sells is False


@pytest.mark.parametrize("name", BOOLEAN_NAMES)
@pytest.mark.parametrize("raw", ["maybe", "2", "tru", "enabled"])
def test_invalid_boolean_names_the_variable(name, raw):
    with pytest.raises(ConfigError, match=f"{name} must be true or false"):
        load(**{name: raw})


@pytest.mark.parametrize("raw,expected", [("5", "5"), ("0.5", "0.5"), (" 7 ", "7"), ("250.25", "250.25")])
def test_max_qty_accepts_positive_numbers(raw, expected):
    assert load(MAX_QTY=raw).max_qty == Decimal(expected)


@pytest.mark.parametrize("raw", ["inf", "Infinity", "nan", "0", "0.0", "-1", "1e3", "abc", "1_0", "1,5"])
def test_max_qty_rejects_everything_else(raw):
    with pytest.raises(ConfigError, match="MAX_QTY must be a positive number"):
        load(MAX_QTY=raw)


@pytest.mark.parametrize("raw,expected", [("2.5", 2.5), ("30", 30.0), (" 1 ", 1.0)])
def test_broker_timeout_accepts_positive_numbers(raw, expected):
    assert load(BROKER_TIMEOUT_SECONDS=raw).broker_timeout_seconds == expected


@pytest.mark.parametrize("raw", ["0", "inf", "nan", "-3", "1e1", "abc"])
def test_broker_timeout_rejects_everything_else(raw):
    with pytest.raises(ConfigError, match="BROKER_TIMEOUT_SECONDS must be a positive number"):
        load(BROKER_TIMEOUT_SECONDS=raw)


@pytest.mark.parametrize("raw,expected", [("0", 0), ("45", 45), (" 5 ", 5)])
def test_dedupe_seconds_accepts_whole_numbers(raw, expected):
    assert load(DEDUPE_SECONDS=raw).dedupe_seconds == expected


@pytest.mark.parametrize("raw", ["-1", "1.5", "abc", "1e2", "1_0"])
def test_dedupe_seconds_rejects_everything_else(raw):
    with pytest.raises(ConfigError, match="DEDUPE_SECONDS"):
        load(DEDUPE_SECONDS=raw)


@pytest.mark.parametrize("raw,expected", [("Tradier", "tradier"), (" SCHWAB ", "schwab"), ("tastytrade", "tastytrade")])
def test_default_broker_is_case_insensitive(raw, expected):
    assert load(DEFAULT_BROKER=raw).default_broker == expected


def test_default_broker_must_be_one_of_the_four():
    with pytest.raises(ConfigError, match="DEFAULT_BROKER must be one of alpaca, tradier, tastytrade, schwab"):
        load(DEFAULT_BROKER="interactive")


def test_log_level_is_upper_cased_and_validated():
    assert load(LOG_LEVEL="debug").log_level == "DEBUG"
    with pytest.raises(ConfigError, match="LOG_LEVEL must be one of"):
        load(LOG_LEVEL="chatty")


def test_allowed_symbols_unset_means_no_restriction():
    assert load().allowed_symbols == ()


def test_allowed_symbols_set_to_empty_string_means_no_restriction():
    assert load(ALLOWED_SYMBOLS="").allowed_symbols == ()


def test_allowed_symbols_are_split_trimmed_upper_cased_and_empty_items_dropped():
    assert load(ALLOWED_SYMBOLS=" aapl, ,msft ,,brk.b ").allowed_symbols == ("AAPL", "MSFT", "BRK.B")


def test_tradingview_ips_unset_means_the_published_list():
    assert load().tradingview_ips == DEFAULT_TRADINGVIEW_IPS


def test_tradingview_ips_set_to_empty_string_means_an_empty_list():
    assert load(TRADINGVIEW_IPS="").tradingview_ips == ()


def test_tradingview_ips_are_split_and_trimmed_but_not_upper_cased():
    assert load(TRADINGVIEW_IPS=" 1.2.3.4 , ,5.6.7.8,abc::1 ").tradingview_ips == ("1.2.3.4", "5.6.7.8", "abc::1")


def test_ips_only_with_an_empty_list_is_refused():
    with pytest.raises(ConfigError, match="TRADINGVIEW_IPS must list at least one address"):
        load(TRADINGVIEW_IPS_ONLY="true", TRADINGVIEW_IPS="")
    with pytest.raises(ConfigError, match="TRADINGVIEW_IPS must list at least one address"):
        load(TRADINGVIEW_IPS_ONLY="true", TRADINGVIEW_IPS=" , ,")


def test_ips_only_with_the_default_list_is_accepted():
    s = load(TRADINGVIEW_IPS_ONLY="true")
    assert s.tradingview_ips_only is True
    assert s.tradingview_ips == DEFAULT_TRADINGVIEW_IPS


def test_empty_ip_list_without_ips_only_is_accepted():
    s = load(TRADINGVIEW_IPS="")
    assert s.tradingview_ips_only is False and s.tradingview_ips == ()


def test_credentials_are_trimmed_and_unset_ones_are_empty():
    s = load(ALPACA_API_KEY_ID="  abc  ", SCHWAB_CALLBACK_URL=" https://127.0.0.1:9000 ", SCHWAB_TOKEN_PATH="t.json")
    assert s.alpaca_api_key_id == "abc"
    assert s.alpaca_api_secret_key == ""
    assert s.schwab_callback_url == "https://127.0.0.1:9000"
    assert s.schwab_token_path == "t.json"


def test_settings_are_frozen():
    s = load()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.dry_run = False  # type: ignore[misc]


def test_repr_never_shows_a_credential():
    credentials = {
        "WEBHOOK_SECRET": "webhook-secret-value-0001",
        "ALPACA_API_KEY_ID": "alpaca-key-value-0002",
        "ALPACA_API_SECRET_KEY": "alpaca-secret-value-0003",
        "TRADIER_ACCESS_TOKEN": "tradier-token-value-0004",
        "TRADIER_ACCOUNT_ID": "tradier-account-value-0005",
        "TASTYTRADE_CLIENT_SECRET": "tasty-secret-value-0006",
        "TASTYTRADE_REFRESH_TOKEN": "tasty-refresh-value-0007",
        "TASTYTRADE_ACCOUNT_NUMBER": "tasty-account-value-0008",
        "SCHWAB_APP_KEY": "schwab-key-value-0009",
        "SCHWAB_APP_SECRET": "schwab-secret-value-0010",
        "SCHWAB_ACCOUNT_NUMBER": "schwab-account-value-0011",
    }
    s = load_settings(credentials)
    for text in (repr(s), str(s), f"{s}", repr([s])):
        for value in credentials.values():
            assert value not in text


def _example_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            env[name] = value
    return env


def test_env_example_lists_exactly_the_settings_variables():
    assert set(_example_env()) == {f.name.upper() for f in dataclasses.fields(Settings)}


def test_env_example_values_match_the_code_defaults():
    env = _example_env()
    env["WEBHOOK_SECRET"] = SECRET
    assert load_settings(env) == load()


def test_every_variable_in_env_example_has_a_comment_line_above_it():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line and not line.startswith("#"):
            assert lines[index - 1].startswith("#"), line
