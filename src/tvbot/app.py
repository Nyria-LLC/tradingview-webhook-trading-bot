"""FastAPI application: the /webhook route and the rails around it.

Status policy: a 5xx other than 504 would make TradingView resend the webhook, and a resend
after a placed order would duplicate it. Failures before an order request is sent, and definite
broker refusals, answer 424; anything after the request reached the network answers 504.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .alert import AlertError, check_secret, parse_alert
from .brokers import make_brokers
from .brokers.base import (
    Broker,
    BrokerRejected,
    GuardRefused,
    NotConfigured,
    OrderIntent,
    OutcomeUnknown,
    dec_str,
)
from .brokers.schwab import SchwabBroker
from .config import BROKER_NAMES, Settings, load_settings
from .logs import Scrubber, setup_logging
from .state import DedupeCache, SymbolLocks

logger = logging.getLogger("tvbot")

MAX_BODY_BYTES = 10240
MAX_BROKER_MESSAGE = 300
UNEXPECTED = "unexpected error, see server log"
REAL_ORDER_WARNING = (
    "order requests follow each broker's published API; no order has been placed through this bot "
    "by its authors. Start with DRY_RUN=true and a paper or sandbox account."
)
BROKER_LABELS = {"alpaca": "Alpaca", "tradier": "Tradier", "tastytrade": "tastytrade", "schwab": "Schwab"}


def _safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run a side effect that must never change the answer."""
    try:
        fn(*args, **kwargs)
    except Exception:
        pass


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


class CatchAllMiddleware:
    """Pure ASGI wrapper: an exception that escapes a route is logged and answered with 424.

    It never re-raises, so nothing reaches the server's own error log or becomes a 500.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            _safe(logger.exception, "unexpected error")
            if not started:
                try:
                    await JSONResponse(_error(UNEXPECTED), status_code=424)(scope, receive, send)
                except Exception:
                    pass


def _unknown_message(broker_name: str, cid: str | None, reason: str) -> str:
    if cid:
        where = f"Check your broker for client id {cid}"
    else:
        where = f"Check your {BROKER_LABELS.get(broker_name, broker_name)} orders."
    return f"outcome unknown ({reason}); the order may have been placed. {where}"


def create_app(
    settings: Settings | None = None,
    http_client: httpx.Client | None = None,
    schwab_client_factory: Callable[[Any], Any] | None = None,
    clock: Callable[[], float] | None = None,
) -> FastAPI:
    """Build the application. Run with `uvicorn tvbot.app:create_app --factory`."""
    settings = settings or load_settings(os.environ)
    scrubber = Scrubber(settings)
    setup_logging(settings, scrubber)

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=settings.broker_timeout_seconds)
    brokers = make_brokers(settings, client, schwab_client_factory, scrubber)
    dedupe = DedupeCache(settings.dedupe_seconds, clock or time.monotonic)
    locks = SymbolLocks()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_client:
                client.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.brokers = brokers
    app.state.dedupe = dedupe
    app.state.scrubber = scrubber
    app.add_middleware(CatchAllMiddleware)

    live = [name for name in BROKER_NAMES if getattr(settings, f"{name}_live")]
    logger.info(
        "tvbot %s dry_run=%s default_broker=%s live=%s",
        __version__,
        settings.dry_run,
        settings.default_broker,
        ",".join(live) if live else "none",
    )
    if not settings.dry_run:
        logger.warning(REAL_ORDER_WARNING)
        schwab = brokers["schwab"]
        if isinstance(schwab, SchwabBroker) and not schwab.missing_env():
            schwab.warm_up()

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            message = "not found"
        elif exc.status_code == 405:
            message = "method not allowed"
        else:
            message = str(exc.detail)
        return JSONResponse(_error(message), status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.get("/health")
    async def health():
        return JSONResponse({"ok": True})

    # request handling, run in a worker thread (steps 4 to 10 of the webhook contract)

    def note(level: int, intent: OrderIntent, status: int, message: str | None = None,
             order_id: str | None = None, cid: str | None = None) -> None:
        text = "alert broker=%s symbol=%s action=%s qty=%s type=%s tif=%s dry_run=%s status=%s order_id=%s client_id=%s"
        args: tuple[Any, ...] = (
            intent.broker,
            intent.symbol,
            intent.side,
            dec_str(intent.quantity),
            intent.order_type,
            intent.time_in_force,
            settings.dry_run,
            status,
            order_id or "-",
            cid or "-",
        )
        if message is not None:
            text += ": %s"
            args += (message,)
        _safe(logger.log, level, text, *args)

    def clean(text: str) -> str:
        try:
            return scrubber.scrub(text)
        except Exception:
            return "details withheld: scrub failed"

    def refuse(intent: OrderIntent, status: int, message: str) -> tuple[int, dict]:
        message = clean(message)
        note(logging.WARNING, intent, status, message)
        return status, _error(message)

    def place(broker: Broker, intent: OrderIntent) -> tuple[int, dict, bool]:
        """Run the order; returns (status, body, keep) where keep says whether to remember the body."""
        missing = broker.missing_env()
        if missing:
            status, body = refuse(intent, 422, f"{intent.broker} is not configured: set " + ", ".join(missing))
            return status, body, False
        sent = False
        try:
            with locks.get(intent.broker, intent.symbol):
                sent = True  # from here an unexpected failure may follow a request that reached the broker
                result = broker.place(intent, settings.clamp_sells)
            body = {
                "ok": True,
                "dry_run": False,
                "broker": intent.broker,
                "order_id": result.order_id,
                "client_id": result.client_id,
                "quantity": result.quantity,
            }
            note(logging.INFO, intent, 200, order_id=result.order_id, cid=result.client_id)
            return 200, body, True
        except (GuardRefused, NotConfigured) as exc:
            status, body = refuse(intent, 422, str(exc))
            return status, body, False
        except BrokerRejected as exc:
            message = clean(exc.message)[:MAX_BROKER_MESSAGE]
            note(logging.WARNING, intent, 424, message)
            return 424, {"ok": False, "error": message, "broker_status": exc.status_code}, False
        except OutcomeUnknown as exc:
            message = clean(_unknown_message(intent.broker, exc.client_id, exc.reason))
            note(logging.WARNING, intent, 504, message, cid=exc.client_id)
            return 504, _error(message), True
        except Exception:
            _safe(logger.exception, "unexpected error placing alert broker=%s symbol=%s", intent.broker, intent.symbol)
            if sent:
                message = clean(_unknown_message(intent.broker, None, "unexpected error"))
                note(logging.WARNING, intent, 504, message)
                return 504, _error(message), True
            note(logging.WARNING, intent, 424, UNEXPECTED)
            return 424, _error(UNEXPECTED), False

    def process(raw: bytes, payload: dict) -> tuple[int, dict]:
        try:
            check_secret(payload, settings)
            intent = parse_alert(payload, settings)
        except AlertError as exc:
            _safe(logger.warning, "webhook refused status=%s: %s", exc.status, exc.message)
            return exc.status, _error(exc.message)
        broker = app.state.brokers[intent.broker]
        try:
            broker.validate(intent)
        except GuardRefused as exc:
            return refuse(intent, 422, str(exc))

        if settings.dry_run:
            try:
                preview = broker.preview(intent)
            except NotConfigured as exc:
                return refuse(intent, 422, str(exc))
            note(logging.INFO, intent, 200)
            return 200, {"ok": True, "dry_run": True, "broker": intent.broker, "request": preview}

        key = hashlib.sha256(raw).hexdigest()
        if not dedupe.reserve(key):
            note(logging.WARNING, intent, 200, "duplicate alert body skipped")
            return 200, {"ok": True, "duplicate": True}
        keep = False
        try:
            status, body, keep = place(broker, intent)
            return status, body
        finally:
            _safe(dedupe.finish, key, keep)

    @app.post("/webhook")
    async def webhook(request: Request):
        if settings.tradingview_ips_only:
            caller = request.client
            if caller is None or caller.host not in settings.tradingview_ips:
                _safe(logger.warning, "webhook refused status=403: client host %s", caller.host if caller else "unknown")
                return JSONResponse(_error("forbidden"), status_code=403)

        declared = request.headers.get("content-length")
        if declared is not None and declared.strip().isdecimal() and int(declared) > MAX_BODY_BYTES:
            _safe(logger.warning, "webhook refused status=413: body too large")
            return JSONResponse(_error("body too large"), status_code=413)
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                _safe(logger.warning, "webhook refused status=413: body too large")
                return JSONResponse(_error("body too large"), status_code=413)
            chunks.append(chunk)
        raw = b"".join(chunks)

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
            payload = None
        if not isinstance(payload, dict):
            _safe(logger.warning, "webhook refused status=400: body must be a JSON object")
            return JSONResponse(_error("body must be a JSON object"), status_code=400)

        status, body = await run_in_threadpool(process, raw, payload)
        return JSONResponse(body, status_code=status)

    return app
