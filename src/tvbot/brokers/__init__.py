"""Broker adapters and the registry that builds one of each."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .alpaca import AlpacaBroker
from .base import (
    Broker,
    BrokerRejected,
    BrokerResult,
    GuardRefused,
    NotConfigured,
    OrderIntent,
    OutcomeUnknown,
)
from .schwab import SchwabBroker, default_client_factory
from .tastytrade import TastytradeBroker
from .tradier import TradierBroker

__all__ = [
    "AlpacaBroker",
    "Broker",
    "BrokerRejected",
    "BrokerResult",
    "GuardRefused",
    "NotConfigured",
    "OrderIntent",
    "OutcomeUnknown",
    "SchwabBroker",
    "TastytradeBroker",
    "TradierBroker",
    "make_brokers",
]


def make_brokers(
    settings: Any,
    http_client: Any,
    schwab_client_factory: Callable[[Any], Any] | None,
    scrubber: Any,
) -> dict[str, Broker]:
    """One adapter per supported broker, keyed by the name an alert uses."""
    return {
        "alpaca": AlpacaBroker(settings, http_client, scrubber=scrubber),
        "tradier": TradierBroker(settings, http_client, scrubber=scrubber),
        "tastytrade": TastytradeBroker(settings, http_client, scrubber=scrubber),
        "schwab": SchwabBroker(settings, schwab_client_factory or default_client_factory, scrubber=scrubber),
    }
