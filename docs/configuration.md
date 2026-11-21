# Configuration

The bot reads its settings from environment variables, once, at startup. Only `WEBHOOK_SECRET` is required. Every other variable has a default, and the defaults place no orders.

The run command loads the variables from `.env`:

```sh
uvicorn tvbot.app:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
```

Copy `.env.example` to `.env` to start. Restart the bot after any change.

## Core settings

| Variable | Default | Meaning |
|---|---|---|
| `WEBHOOK_SECRET` | none, required | The shared secret, at least 16 characters. It must equal the alert's `secret`. |
| `DRY_RUN` | `true` | When true, no broker is called. The response shows the request that would be sent. |
| `DEFAULT_BROKER` | `alpaca` | The broker used when the alert has no `broker` field: `alpaca`, `tradier`, `tastytrade` or `schwab`. |
| `MAX_QTY` | `100` | The largest quantity, in shares, accepted in one alert. |
| `ALLOWED_SYMBOLS` | empty | A comma-separated list. When set, other symbols are refused. |
| `CLAMP_SELLS` | `false` | When true, a sell larger than the long position sells the position instead. Applies to Alpaca, Tradier and tastytrade. |
| `DEDUPE_SECONDS` | `30` | The window of the duplicate guard, in seconds. `0` turns the guard off. |
| `TRADINGVIEW_IPS_ONLY` | `false` | When true, only the addresses in `TRADINGVIEW_IPS` may call `/webhook`. |
| `TRADINGVIEW_IPS` | `52.89.214.238,34.212.75.30,54.218.53.128,52.32.178.7` | TradingView's published webhook addresses. |
| `BROKER_TIMEOUT_SECONDS` | `10` | The timeout for each Alpaca, Tradier and tastytrade HTTP call. |
| `LOG_LEVEL` | `INFO` | The logging level of the `tvbot` logger. |

`BROKER_TIMEOUT_SECONDS` does not apply to Charles Schwab. schwab-py uses its own timeout of 30 seconds.

## Alpaca

| Variable | Default | Meaning |
|---|---|---|
| `ALPACA_API_KEY_ID` | none | Your Alpaca API key id. Use paper keys unless `ALPACA_LIVE=true`. |
| `ALPACA_API_SECRET_KEY` | none | The secret key that pairs with the key id. |
| `ALPACA_LIVE` | `false` | True sends orders to `https://api.alpaca.markets`. False sends them to `https://paper-api.alpaca.markets`. |

Guide: [How to send TradingView alerts to Alpaca](alpaca.md).

## Tradier

| Variable | Default | Meaning |
|---|---|---|
| `TRADIER_ACCESS_TOKEN` | none | Your Tradier access token. |
| `TRADIER_ACCOUNT_ID` | none | The id of the Tradier account the bot trades. |
| `TRADIER_LIVE` | `false` | True sends orders to `https://api.tradier.com/v1`. False sends them to `https://sandbox.tradier.com/v1`. |

Guide: [How to send TradingView alerts to Tradier](tradier.md).

## tastytrade

| Variable | Default | Meaning |
|---|---|---|
| `TASTYTRADE_CLIENT_SECRET` | none | The client secret of your tastytrade OAuth application. |
| `TASTYTRADE_REFRESH_TOKEN` | none | The refresh token of your grant. |
| `TASTYTRADE_ACCOUNT_NUMBER` | none | The tastytrade account number the bot trades. |
| `TASTYTRADE_LIVE` | `false` | True sends orders to `https://api.tastyworks.com`. False sends them to `https://api.cert.tastyworks.com`. |

Guide: [How to send TradingView alerts to tastytrade](tastytrade.md).

## Charles Schwab

| Variable | Default | Meaning |
|---|---|---|
| `SCHWAB_APP_KEY` | none | The key of your Schwab developer app. |
| `SCHWAB_APP_SECRET` | none | The secret of your Schwab developer app. |
| `SCHWAB_ACCOUNT_NUMBER` | none | The account number the bot trades. |
| `SCHWAB_CALLBACK_URL` | `https://127.0.0.1:8182` | Must equal the callback URL registered on the Schwab app. |
| `SCHWAB_TOKEN_PATH` | `schwab_token.json` | The token file written by `python -m tvbot.schwab_login`. |
| `SCHWAB_LIVE` | `false` | Must be true for any Schwab order outside dry run. This bot has no Schwab paper mode. |
| `SCHWAB_ALLOW_UNCHECKED_SELLS` | `false` | Must be true for Schwab sells, which the bot cannot check against your position. |

Guide: [How to send TradingView alerts to Charles Schwab](schwab.md).

## How values are read

- **Booleans** ignore case and surrounding spaces. `1`, `true`, `yes` and `on` mean true. `0`, `false`, `no` and `off` mean false. An empty value means the default. Any other value stops startup with an error that names the variable.
- **`MAX_QTY` and `BROKER_TIMEOUT_SECONDS`** must be positive numbers.
- **`DEDUPE_SECONDS`** must be a whole number, zero or more.
- **`DEFAULT_BROKER`** must be one of the four broker names.
- **`ALLOWED_SYMBOLS` and `TRADINGVIEW_IPS`** are split on commas. Spaces around each item are trimmed, empty items are dropped, and symbols are upper-cased.
- **`TRADINGVIEW_IPS_ONLY=true`** with an empty `TRADINGVIEW_IPS` stops startup with an error.
- **`WEBHOOK_SECRET`** missing, or shorter than 16 characters, stops startup with this message: `WEBHOOK_SECRET must be set to at least 16 characters`. The example value from `.env.example` is refused too, with this message: `WEBHOOK_SECRET is still the placeholder from .env.example; generate a real one`.

## The startup log line

At startup the bot logs its version, the dry-run state, the default broker and the brokers set to live:

```
tvbot 0.1.0 dry_run=<bool> default_broker=<broker> live=<brokers whose *_LIVE is true, or none>
```

When `DRY_RUN` is false it also logs this warning:

```
order requests follow each broker's published API; no order has been placed through this bot by its authors. Start with DRY_RUN=true and a paper or sandbox account.
```
