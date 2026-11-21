# TradingView webhook trading bot

A self-hosted TradingView webhook bot, written in Python with FastAPI. It turns a TradingView alert into a stock or ETF order at Alpaca, Tradier, tastytrade or Charles Schwab. It starts in dry run, and a live order needs that broker's own flag. MIT licensed.

## How do I automate TradingView alerts to my broker?

Point the alert's webhook at a program that places the order through your broker's API. TradingView posts the alert message to a URL you choose. This bot is the program at that URL, and you run it yourself with your own broker credentials.

1. Your alert fires, and TradingView posts its JSON message to `https://<your-host>/webhook`.
2. The bot checks the shared secret, then the alert's fields.
3. It sends a market or limit order to the broker the alert names.
4. It answers with the broker's order id, or with the reason it refused.

| Broker | Guide | With `DRY_RUN=false`, orders go to | Quantity |
|---|---|---|---|
| Alpaca | [TradingView alerts to Alpaca](docs/alpaca.md) | Alpaca's paper endpoint until `ALPACA_LIVE=true` | Whole shares, or fractions on market day orders |
| Tradier | [TradingView alerts to Tradier](docs/tradier.md) | Tradier's sandbox host until `TRADIER_LIVE=true` | Whole shares |
| tastytrade | [TradingView alerts to tastytrade](docs/tastytrade.md) | tastytrade's sandbox host until `TASTYTRADE_LIVE=true` | Whole shares |
| Charles Schwab | [TradingView alerts to Charles Schwab](docs/schwab.md) | Nowhere until `SCHWAB_LIVE=true`. This bot has no Schwab paper mode | Whole shares |

The bot places market and limit orders for stocks and ETFs, as `day` or `gtc`. A buy opens or adds to a position, and a sell closes one. Options, short selling, crypto and futures are outside this version.

## Quick start

The quick start ends with a dry-run order on your own machine. It needs no broker account.

Requires Python 3.11 or newer; tested on Python 3.11 and 3.14.

1. Clone this repository and install it from its folder:

   ```sh
   python -m pip install .
   ```

   For Charles Schwab, install the extra instead: `python -m pip install ".[schwab]"`.

2. Copy `.env.example` to `.env`.

3. Generate a secret and set it as `WEBHOOK_SECRET` in `.env`:

   ```sh
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   The bot won't start without a secret of at least 16 characters.

4. Run the bot. It listens on 127.0.0.1 only:

   ```sh
   uvicorn tvbot.app:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
   ```

5. Send a test alert. Replace `...` with your secret:

   ```sh
   curl -X POST http://127.0.0.1:8000/webhook \
     -H "Content-Type: application/json" \
     -d '{"secret": "...", "symbol": "AAPL", "action": "buy", "quantity": 1}'
   ```

   `DRY_RUN=true` is the default, so nothing reaches a broker. The bot answers 200 with the request it would send:

   ```json
   {
     "ok": true,
     "dry_run": true,
     "broker": "alpaca",
     "request": {
       "method": "POST",
       "url": "https://paper-api.alpaca.markets/v2/orders",
       "json": {
         "symbol": "AAPL",
         "qty": "1",
         "side": "buy",
         "type": "market",
         "time_in_force": "day",
         "client_order_id": "tvbot-5f0c2a9b7d3e4a1c8b6f2d0e"
       }
     }
   }
   ```

6. Point TradingView at it. TradingView has to reach the bot over the internet on port 80 or 443, so follow [Running it on the internet](#running-it-on-the-internet) first. Then create an alert, set its webhook URL to `https://<your-host>/webhook`, and paste a message from [The alert message](#the-alert-message) into the alert's Message field. TradingView's [webhook article](https://www.tradingview.com/support/solutions/43000529348-about-webhooks/) covers the alert settings.

7. Open your broker's guide under [Broker guides](#broker-guides). It covers credentials, a paper or sandbox run, and going live.

## The alert message

The alert message is one JSON object, and it's the same for every broker. The shared secret travels in the body because TradingView cannot set custom headers. Two ready-made messages are in `examples/`.

For an indicator alert, use [examples/tradingview-alert-indicator.json](examples/tradingview-alert-indicator.json). Indicator alerts cannot use strategy placeholders, so the action and quantity are written in. Create a second alert with `"action": "sell"` for the exit.

```json
{
  "secret": "paste-your-WEBHOOK_SECRET-here",
  "alert_id": "{{timenow}}",
  "broker": "alpaca",
  "symbol": "{{ticker}}",
  "action": "buy",
  "quantity": 1,
  "order_type": "market",
  "time_in_force": "day"
}
```

For a strategy alert, use [examples/tradingview-alert-strategy.json](examples/tradingview-alert-strategy.json). TradingView fills in the action and quantity from the strategy's order.

```json
{
  "secret": "paste-your-WEBHOOK_SECRET-here",
  "alert_id": "{{timenow}}",
  "broker": "alpaca",
  "symbol": "{{ticker}}",
  "action": "{{strategy.order.action}}",
  "quantity": "{{strategy.order.contracts}}",
  "order_type": "market",
  "time_in_force": "day"
}
```

| Field | Required | Rule |
|---|---|---|
| `secret` | Yes | Must equal `WEBHOOK_SECRET`. The bot checks it before any other field. |
| `alert_id` | No | Not part of the order. It makes each firing's body unique for the duplicate guard. |
| `broker` | No | `alpaca`, `tradier`, `tastytrade` or `schwab`. Defaults to `DEFAULT_BROKER`. |
| `symbol` | Yes | Starts with a letter, then letters, digits or dots, 10 characters at most. Text before a colon is dropped, so `NASDAQ:AAPL` becomes `AAPL`. |
| `action` | Yes | `buy` or `sell`. |
| `quantity` | Yes | Shares. A positive number, or a string such as `"10"` or `"2.5"`. No more than `MAX_QTY`. |
| `order_type` | No | `market` or `limit`. Defaults to `market`. |
| `limit_price` | Limit orders | A positive number or numeric string. Ignored on market orders. |
| `time_in_force` | No | `day` or `gtc`. Defaults to `day`. |

Upper and lower case both work for `broker`, `symbol`, `action`, `order_type` and `time_in_force`. Fields the bot doesn't know are ignored.

Share-class tickers such as BRK.B pass through as sent. If your broker spells the symbol differently in its position list, the bot refuses the sell instead of guessing.

For a limit order, set `"order_type": "limit"` and add a price, such as `"limit_price": "{{close}}"`. The bot passes the price to the broker as given and does not round it.

`alert_id` exists for the duplicate guard. `{{timenow}}` is the alert's fire time to the nearest second, in UTC, such as `2019-08-27T09:56:00Z`. Separate firings get different bodies. A resend of one firing matches the first and is skipped. Keep the field in every alert.

Reversing strategies need one change. When a strategy flips from long to short, TradingView puts the close and the new position into one order. A strategy holding 10 shares can send a sell of 20. At Alpaca, Tradier and tastytrade, the bot refuses a sell larger than your position, and a refused sell leaves the position open. Pick one fix:

- Restrict the strategy to long trades with `strategy.risk.allow_entry_in(strategy.direction.long)`. TradingView's [strategy docs](https://www.tradingview.com/pine-script-docs/concepts/strategies/) say the strategy then "enters trades only in that direction", and an opposite entry "generates a market order to close the position without entering a new trade in that direction".
- Set `CLAMP_SELLS=true`. The bot then sells the shares you hold. This works at Alpaca, Tradier and tastytrade.

At Charles Schwab the bot cannot check or clamp a sell, so use the Pine setting there.

## What TradingView requires

TradingView sends webhooks on paid plans only, with two-factor authentication on, to ports 80 and 443. It cancels a request that takes longer than three seconds.

| Requirement | Detail | Source |
|---|---|---|
| Plan | Webhooks need a paid plan, Essential or higher. | [Pricing](https://www.tradingview.com/pricing/) |
| Two-factor authentication | "webhook alerts are only allowed when 2-factor authentication is enabled." | [Webhooks](https://www.tradingview.com/support/solutions/43000529348-about-webhooks/) |
| Ports | "only ports 80 and 443 are accepted. Requests to other ports will be rejected." | [Webhooks](https://www.tradingview.com/support/solutions/43000529348-about-webhooks/) |
| IPv6 | "IPv6 isn't currently supported for webhooks." | [Webhooks](https://www.tradingview.com/support/solutions/43000529348-about-webhooks/) |
| Time limit | "If a remote server takes longer than three seconds to process a request, the request will be cancelled." | [Webhooks](https://www.tradingview.com/support/solutions/43000529348-about-webhooks/) |
| Content type | A message that is valid JSON arrives as `application/json`. Anything else arrives as `text/plain`. | [Webhooks](https://www.tradingview.com/support/solutions/43000529348-about-webhooks/) |
| Resends | On a response status from 500 to 599, except 504, TradingView sends the webhook again after 5 seconds, up to 3 times. | [Webhook resubmission](https://www.tradingview.com/support/solutions/43000735201-webhook-resubmission/) |
| Source addresses | `52.89.214.238`, `34.212.75.30`, `54.218.53.128` and `52.32.178.7`. | [Webhooks](https://www.tradingview.com/support/solutions/43000529348-about-webhooks/) |

The example messages use these placeholders, described in TradingView's [placeholder article](https://www.tradingview.com/support/solutions/43000531021-how-to-use-a-variable-value-in-alert/):

| Placeholder | Value |
|---|---|
| `{{ticker}}` | The alert's symbol, such as AAPL. |
| `{{close}}` | The close of the bar that triggered the alert. |
| `{{timenow}}` | The alert's fire time, to the nearest second. |
| `{{strategy.order.action}}` | "buy" or "sell" for the executed order. Strategy alerts only. |
| `{{strategy.order.contracts}}` | The number of contracts of the executed order. Strategy alerts only. |

TradingView also says: "Our alerts are **not designed for automated trading**." The sentence is in its article on [using credentials for webhooks](https://www.tradingview.com/support/solutions/43000722015-using-credentials-for-webhooks/).

## Broker guides

Each guide covers one broker from credentials to a live order.

- [How to send TradingView alerts to Alpaca](docs/alpaca.md)
- [How to send TradingView alerts to Tradier](docs/tradier.md)
- [How to send TradingView alerts to tastytrade](docs/tastytrade.md)
- [How to send TradingView alerts to Charles Schwab](docs/schwab.md)
- [Configuration](docs/configuration.md) lists every environment variable and its default.

## What has been tested

The bot is tested with mocked broker responses, not with real orders.

- Alpaca: requests follow Alpaca's API reference and the recorded responses in Alpaca's own SDK tests. A read-only request to an Alpaca paper account's positions endpoint succeeded.
- Tradier: requests follow Tradier's API reference.
- tastytrade: requests follow tastytrade's API reference and OpenAPI schema.
- Charles Schwab: orders are built with schwab-py's own order builders.

Every broker request is checked by tests with mocked responses. No order has been placed through this bot by its authors. Start with DRY_RUN=true, then a paper or sandbox account.

The bot logs the same caution at startup whenever `DRY_RUN` is false. To run the tests yourself:

```sh
python -m pip install -e ".[dev,schwab]"
python -m pytest -q
```

## Safety defaults

Out of the box the bot places no order. Each step toward a live order is a setting you change yourself.

| Setting | Default | Effect |
|---|---|---|
| `DRY_RUN` | `true` | No broker is called. The response shows the request the bot would send. |
| `ALPACA_LIVE`, `TRADIER_LIVE`, `TASTYTRADE_LIVE` | `false` | Orders go to the broker's paper or sandbox endpoint until the flag is `true`. |
| `SCHWAB_LIVE` | `false` | This bot has no Schwab paper mode, so every Schwab order outside dry run needs `SCHWAB_LIVE=true`. |
| `SCHWAB_ALLOW_UNCHECKED_SELLS` | `false` | Schwab sells are refused until this is `true`. |
| `MAX_QTY` | `100` | The largest quantity one alert can order. It counts shares, not dollars. |
| `ALLOWED_SYMBOLS` | empty | When set, the bot refuses every symbol not on the list. |
| `CLAMP_SELLS` | `false` | When `true`, a sell larger than your long position sells the position instead. |
| `DEDUPE_SECONDS` | `30` | The duplicate guard's window. `0` turns the guard off. |
| `TRADINGVIEW_IPS_ONLY` | `false` | When `true`, only TradingView's published addresses can call `/webhook`. |

MAX_QTY limits one alert only. The bot has no limit on total position size or on alerts per minute, so use your broker's own limits too.

Sells at Alpaca, Tradier and tastytrade are checked against your long position first. A sell with no long position is refused. A sell larger than the position is refused too, or reduced to the position when `CLAMP_SELLS=true`. So the bot does not open shorts at those three brokers. `CLAMP_SELLS` applies to them only.

The check has limits. At Tradier and tastytrade it does not subtract open sell orders. A refused sell is not retried. A buy is always sent, even if you hold a short placed outside the bot.

Charles Schwab works differently. The bot cannot read your position there, so sells are refused unless `SCHWAB_ALLOW_UNCHECKED_SELLS=true`. With that flag on, a sell goes out as the broker's SELL instruction, never SELL_SHORT, with no position check. What Charles Schwab does with a SELL for shares you do not hold is not documented in our sources.

The duplicate guard runs whenever `DRY_RUN` is false. Identical bodies inside `DEDUPE_SECONDS` are skipped: the second one gets `{"ok": true, "duplicate": true}` and no broker call. A body is remembered while its first request is running, and after a 200 or a 504. After any other status it is forgotten, so you can send the same alert again at once. Dry runs skip the guard because they place nothing.

Run a single uvicorn worker. The duplicate guard, the per-symbol locks and the broker token caches live in that one process's memory.

The shared secret authenticates every alert. The bot checks it before it validates any field, so a caller without it learns nothing about your alert rules. `TRADINGVIEW_IPS_ONLY` is defense in depth: a second gate in front of the secret, which is still required. Credentials stay out of the bot's output. Dry-run previews show placeholders such as `<TRADIER_ACCOUNT_ID>`, and log lines are scrubbed of every configured secret.

## Running it on the internet

Keep the bot on 127.0.0.1 and put an HTTPS reverse proxy or tunnel in front of it on port 443. TradingView sends only to ports 80 and 443, and port 80 would carry your secret in clear text.

1. Run one process with one worker, bound to 127.0.0.1.
2. Terminate HTTPS at the proxy or tunnel on port 443 and forward to `http://127.0.0.1:8000`.
3. Set the proxy's upstream timeout to at least 120 seconds, so it waits for the bot's answer. Why 120 seconds: a tastytrade sell with a token refresh can take about 70 seconds at 10 seconds per call, and a Schwab order up to about 90 seconds at schwab-py's 30 seconds per call.
4. Turn off proxy retries for POST, so one alert reaches the bot once.
5. Keep a request-size limit at the proxy. The bot itself refuses bodies over 10,240 bytes.
6. Use a proxy that buffers the request body and closes slow uploads; the bot itself sets no read deadline.
7. Generate the secret with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and treat it like a password. Anyone who has it can place orders.

To use `TRADINGVIEW_IPS_ONLY=true` behind a proxy on the same machine, start uvicorn with `--proxy-headers --forwarded-allow-ips 127.0.0.1`. Never use `--forwarded-allow-ips '*'`: any caller could then pass the IP gate. If TradingView changes its addresses, alerts get 403 until you update `TRADINGVIEW_IPS` from TradingView's [webhook article](https://www.tradingview.com/support/solutions/43000529348-about-webhooks/). The IP gate is optional. Before you rely on it, check that real alerts still get through your proxy or tunnel.

Two brokers need extra care on a server. The tastytrade adapter's first order after a start makes up to two extra calls: a token request, and a position read before a sell. Charles Schwab needs `python -m tvbot.schwab_login` every week, and its token file must stay private. After the weekly Schwab login, the bot picks up the new token file on the next order.

When the bot answers 424 because it could not reach the broker, TradingView doesn't retry that alert, so a network blip loses it. Check the alert log and send it again yourself.

Logs are scrubbed of the secrets the bot knows about, but treat them as private anyway.

Some duplicate-order risk remains. A proxy that answers 502 on its own after the order went out makes TradingView resend. The duplicate guard skips that resend inside its window, but not after the window or after a restart. The reverse case exists too: if two identical bodies arrive together and the first then fails, the second was already answered as a duplicate, and no order is placed.

## Responses

Every response is JSON with an `ok` field. The bot never answers with a 5xx status other than 504, because TradingView resends on 500 to 599 except 504, and a resend after a placed order would duplicate it.

| Status | Body | When |
|---|---|---|
| 200 | `{"ok": true, "dry_run": true, "broker": "<name>", "request": {...}}` | Dry run. `request` is what the bot would send. |
| 200 | `{"ok": true, "dry_run": false, "broker": "<name>", "order_id": "<id or null>", "client_id": "<id or null>", "quantity": "<placed quantity>"}` | The broker accepted the order request. |
| 200 | `{"ok": true, "duplicate": true}` | Same body as a recent alert. No broker call. |
| 400 | `{"ok": false, "error": "body must be a JSON object"}` | The body is not valid UTF-8 JSON, or not a JSON object. |
| 401 | `{"ok": false, "error": "unauthorized"}` | `secret` is missing or wrong. |
| 403 | `{"ok": false, "error": "forbidden"}` | `TRADINGVIEW_IPS_ONLY` is on and the caller is not in `TRADINGVIEW_IPS`. |
| 404 | `{"ok": false, "error": "not found"}` | Unknown path. |
| 405 | `{"ok": false, "error": "method not allowed"}` | Wrong method, such as GET on `/webhook`. |
| 413 | `{"ok": false, "error": "body too large"}` | The body is over 10,240 bytes. |
| 422 | `{"ok": false, "error": "<message>"}` | The alert broke a rule, a guard refused the order, or the broker is not configured. |
| 424 | `{"ok": false, "error": "<broker message>", "broker_status": <status or null>}` | The broker refused the order, or the bot stopped before sending it, for example because it could not reach the broker. |
| 424 | `{"ok": false, "error": "unexpected error, see server log"}` | An unexpected error. The details are in the bot's log. |
| 504 | `{"ok": false, "error": "outcome unknown (<reason>); the order may have been placed. Check your broker for client id <id>"}` | A timeout, a broken connection or a broker 5xx after the order was sent. Check the broker before you resend. |

Checks run in a fixed order, and the first failure decides the status: IP gate, body size, JSON, secret, alert fields, broker rules. `GET /health` answers 200 with `{"ok": true}`. Error statuses show as failures in the "Webhook status" column of TradingView's alert log.

Each order carries a client id that starts with `tvbot-`, so you can find it at Alpaca, Tradier or tastytrade after a 504. Charles Schwab orders have no client id, and the 504 message ends with "Check your Schwab orders."

The 422 messages you are most likely to see:

| Message | Cause |
|---|---|
| `unknown broker` | `broker` is not one of the four names. |
| `invalid symbol` | `symbol` is missing or breaks the symbol rule. |
| `symbol not allowed` | `ALLOWED_SYMBOLS` is set and the symbol is not on it. |
| `action must be buy or sell` | `action` is missing or another word. |
| `quantity must be a positive number` | `quantity` is missing, zero, negative or not a plain number. |
| `quantity exceeds MAX_QTY` | The alert asks for more shares than `MAX_QTY`. |
| `order_type must be market or limit` | Another order type was sent. |
| `limit_price is required for limit orders` | A limit order has no usable `limit_price`. |
| `time_in_force must be day or gtc` | Another time in force was sent. |
| `<broker> is not configured: set <VAR>, <VAR>` | `DRY_RUN` is false and the broker's variables are missing. |
| `no long position in <symbol> to sell; this bot does not open short positions` | A sell arrived with no long position at Alpaca, Tradier or tastytrade. |
| `sell quantity <quantity> exceeds the <held> shares held in <symbol>` | The sell is larger than the position and `CLAMP_SELLS` is off. |

Each broker guide lists that broker's own messages.

## FAQ

### Can I paper trade a TradingView strategy for free?

Not entirely. TradingView sends webhooks on paid plans only, Essential or higher. The bot itself is MIT licensed and costs nothing. With `DRY_RUN=false` it sends orders to Alpaca's paper endpoint, or to the Tradier or tastytrade sandbox host, until you set that broker's live flag. This bot has no Schwab paper mode.

### Can it trade options?

No. This version places stock and ETF orders only. Short selling, crypto and futures are out of it too. Nyria, the hosted alternative described below, handles options.

### Can Discord or Telegram alerts trigger it?

No. The bot has one input: a JSON POST to `/webhook` that carries your secret. TradingView sends that, and so can your own script. Nothing in the bot reads Discord or Telegram messages. Nyria turns Discord and Telegram alerts into orders in your own brokerage account.

### Does TradingView support automated trading?

TradingView's own words are: "Our alerts are **not designed for automated trading**." They come from its article on [using credentials for webhooks](https://www.tradingview.com/support/solutions/43000722015-using-credentials-for-webhooks/). What TradingView provides is the webhook: a POST request with the alert message, sent when the alert triggers. Its [strategy docs](https://www.tradingview.com/pine-script-docs/concepts/strategies/) say order fill alerts "are often more suitable for sending alerts to third parties for automation". TradingView also notes that webhooks "may occasionally fail to reach the specified URL", so check the alert log and your broker.

### What happens if TradingView times out?

TradingView cancels a webhook request after three seconds, and the bot finishes the broker call anyway. The order request runs to its end, and the result is in the bot's log. TradingView's alert log can show a timeout for an order that was placed, so check your broker.

TradingView's resend rule covers 500 to 599 responses except 504. It documents no resend after a timeout. Each Alpaca, Tradier and tastytrade call waits up to `BROKER_TIMEOUT_SECONDS`, 10 by default, and schwab-py uses its own 30 seconds. The tests do not cover the case where TradingView disconnects mid-request.

## Prefer not to run a server?

[Nyria](https://nyria.io/guides/webhook-trading-bot-for-stocks?utm_source=github&utm_medium=readme&utm_campaign=tradingview-webhook-trading-bot) is the hosted alternative. It turns TradingView, Discord and Telegram alerts into orders in your own brokerage account. It handles options, and it has a free paper account to test first. Nyria maintains this repository.

## License and disclaimer

MIT License. Copyright (c) 2026 Nyria LLC. The full text is in [LICENSE](LICENSE).

This project is software, not a broker-dealer or investment adviser, and it never holds or moves money. Trading involves risk of loss.
