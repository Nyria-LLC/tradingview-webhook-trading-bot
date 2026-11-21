# How to send TradingView alerts to Charles Schwab

To send TradingView alerts to Charles Schwab, point the alert's webhook at this bot, install the bot's `schwab` extra and log in with `python -m tvbot.schwab_login`, then repeat that login every week. The bot builds each stock or ETF order with schwab-py and sends it to your account. This bot has no Schwab paper mode. Outside dry run, every order here is a real order, and it needs `SCHWAB_LIVE=true`.

## Prerequisites

- The bot installed with the extra: `python -m pip install ".[schwab]"`. The [quick start](../README.md#quick-start) covers the rest of the install.
- A TradingView plan with webhooks, Essential or higher, and two-factor authentication on.
- A Charles Schwab account, and a developer app from Charles Schwab's [developer portal](https://developer.schwab.com/).

## Credentials

The bot needs four things: the app key, the app secret, the app's callback URL and your account number. Sign-in runs through [schwab-py](https://schwab-py.readthedocs.io/en/latest/auth.html), and the quotes below come from its docs.

- **App key and secret.** They belong to your developer app. schwab-py describes the secret as "provided upon app approval".
- **Callback URL.** `SCHWAB_CALLBACK_URL` has to equal the callback URL registered on the app. schwab-py says it "must *exactly* match the value you've entered in your application configuration, otherwise login will fail with a security error. Be sure to check case and trailing slashes." schwab-py recommends `https://127.0.0.1:8182`, which is the bot's default.
- **Account number.** Set `SCHWAB_ACCOUNT_NUMBER` to the account the bot trades. The login command lists the accounts your login can trade.

Then log in. Run this from the bot's folder, where `.env` is:

```sh
python -m tvbot.schwab_login
```

The command uses schwab-py's manual flow, which "Walks the user through performing an OAuth login flow by manually copy-pasting URLs". When it finishes it prints:

```
Saved the Schwab token to <path>. Schwab tokens last seven days; run this command again when orders start failing with an auth error.
```

It also prints your account numbers, masked to their last 3 characters, such as `...789`.

The token file is a credential. Keep it private. schwab-py says "Tokens are only good for seven days", so run the login every week. The bot picks up the new token file on the next order. Access tokens last thirty minutes, and schwab-py refreshes them for you.

## The .env lines

```
DEFAULT_BROKER=schwab
SCHWAB_APP_KEY=your-schwab-app-key
SCHWAB_APP_SECRET=your-schwab-app-secret
SCHWAB_ACCOUNT_NUMBER=your-schwab-account-number
SCHWAB_CALLBACK_URL=https://127.0.0.1:8182
SCHWAB_TOKEN_PATH=schwab_token.json
SCHWAB_LIVE=false
SCHWAB_ALLOW_UNCHECKED_SELLS=false
```

`DEFAULT_BROKER` picks the broker for alerts that have no `broker` field. `SCHWAB_TOKEN_PATH` is the token file the login command writes. The bot reads `.env` once at startup, so restart it after a change. [Configuration](configuration.md) describes every variable.

## Dry-run test

With `DRY_RUN=true` the bot shows the order it would send and sends nothing. This step needs the `schwab` extra installed. It needs no keys and no login. Replace `...` with your `WEBHOOK_SECRET`:

```sh
curl -X POST http://127.0.0.1:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"secret": "...", "broker": "schwab", "symbol": "AAPL", "action": "buy", "quantity": 1}'
```

The bot answers 200. The response names the broker and the request:

- `"broker"` is `"schwab"`.
- `request.url` is `https://api.schwabapi.com/trader/v1/accounts/<SCHWAB_ACCOUNT_HASH>/orders`. `<SCHWAB_ACCOUNT_HASH>` is a placeholder. The bot never shows the real hash.
- `request.json` is the order exactly as schwab-py builds it: order type MARKET or LIMIT, duration DAY or GOOD_TILL_CANCEL, and the instruction BUY or SELL with the symbol and quantity.

A dry run previews a sell too. `SCHWAB_LIVE` and `SCHWAB_ALLOW_UNCHECKED_SELLS` apply only when an order is about to be placed.

## No paper mode

This bot has no Schwab paper mode. Dry run is the only rehearsal here.

With `DRY_RUN=false`, `SCHWAB_LIVE=false` and the other Schwab variables set, the bot answers every order for this broker with 422:

```
Schwab orders from this bot are real orders; set SCHWAB_LIVE=true to place them
```

To rehearse your alerts with paper orders, send them to another broker first. The alert message is the same for every broker, so only `"broker"` changes. The [Alpaca](alpaca.md), [Tradier](tradier.md) and [tastytrade](tastytrade.md) guides cover their paper and sandbox runs.

## Going live

Every order from this point is a real order in your account.

1. Set a small `MAX_QTY` and an `ALLOWED_SYMBOLS` list.
2. Run `python -m tvbot.schwab_login`, if you haven't in the last week.
3. Set `DRY_RUN=false` and `SCHWAB_LIVE=true`. `DRY_RUN` is one switch for every broker.
4. Restart the bot. Its startup log line names `schwab` after `live=`.
5. Point your TradingView alert at the bot. [Running it on the internet](../README.md#running-it-on-the-internet) covers the HTTPS setup, and [The alert message](../README.md#the-alert-message) has the JSON to paste. Change `"broker"` to `"schwab"`, or set `DEFAULT_BROKER=schwab` and drop the field.

From then on, buys go to your account. Sells stay off until you turn them on:

- Charles Schwab sells need `SCHWAB_ALLOW_UNCHECKED_SELLS=true`. Without it, the bot refuses every sell for this broker with 422.
- The bot cannot check your position at this broker. With the flag on, it sends each sell as a SELL order without a position check.
- The bot uses the BUY and SELL instructions only. schwab-py describes SELL as "Close a long equity position". The bot does not use SELL_SHORT or BUY_TO_COVER.
- What Charles Schwab does with a SELL for shares you do not hold is not documented in our sources. Keep your sell alerts in step with your position.
- `CLAMP_SELLS` does not apply here. For a strategy that reverses, restrict it to long trades in Pine, as [The alert message](../README.md#the-alert-message) describes.

## Charles Schwab notes

- Orders are whole shares in this bot. It answers 422 for a fractional quantity.
- `BROKER_TIMEOUT_SECONDS` does not apply here. schwab-py uses its own timeout: "schwab-py defaults to 30 seconds".
- Orders have no client id, so `client_id` is null in the response. `order_id` can be null too, when the bot cannot read the id from the broker's answer. A 200 still means the order request was accepted. It is not a fill.
- After a 504 the message ends with "Check your Schwab orders." The order may have been placed, so look before you resend.
- When the token is over six days old, the bot logs a warning at startup and after each order: `Schwab token is <n> days old; run python -m tvbot.schwab_login before it expires at 7 days`.
- A buy is always sent, even if you hold a short placed outside the bot.

## What has been tested

- Charles Schwab: orders are built with schwab-py's own order builders.

Every broker request is checked by tests with mocked responses. No order has been placed through this bot by its authors. Start with DRY_RUN=true.

## Troubleshooting

| Status | Error | What to do |
|---|---|---|
| 401 | `unauthorized` | Make the alert's `secret` equal `WEBHOOK_SECRET`. |
| 422 | `install the Schwab extra: python -m pip install ".[schwab]"` | Run that install command and restart. |
| 422 | `<broker> is not configured: set <VAR>, <VAR>` | `DRY_RUN` is false and a value is missing. The message names it: `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET` or `SCHWAB_ACCOUNT_NUMBER`. Add it to `.env` and restart. |
| 422 | `Schwab orders from this bot are real orders; set SCHWAB_LIVE=true to place them` | Set `SCHWAB_LIVE=true` when you are ready for real orders. |
| 422 | `Schwab sells are off: this bot cannot check your Schwab position. Set SCHWAB_ALLOW_UNCHECKED_SELLS=true to send sells as Schwab SELL orders` | Read [Going live](#going-live), then set the flag if you accept unchecked sells. |
| 422 | `Schwab orders must be whole shares in this bot` | Send a whole quantity. |
| 422 | `run python -m tvbot.schwab_login first; SCHWAB_TOKEN_PATH was not found` | Run the login command. Check `SCHWAB_TOKEN_PATH` points at the file it wrote. |
| 422 | `SCHWAB_ACCOUNT_NUMBER is not one of the accounts this Schwab login can trade` | Compare `SCHWAB_ACCOUNT_NUMBER` with the accounts the login command printed. |
| 424 | `"broker_status": <status>` | The broker refused the request. The error text carries its answer. |
| 424 | `"broker_status": null` | The bot could not reach the broker, or your Schwab login has expired. No order was sent. Send the alert again. |
| 504 | `outcome unknown (...); the order may have been placed. Check your Schwab orders.` | Check your orders at the broker before you resend. |

If orders start failing with an auth error about a week after your last login, the token has expired. schwab-py says "requests for a new access token using a refresh token older than seven days are rejected with an invalid_client error". Run `python -m tvbot.schwab_login` again. The bot picks up the new token file on the next order.

The login command reports its own failures:

| Output | Meaning |
|---|---|
| `missing <NAME>` | `SCHWAB_APP_KEY` or `SCHWAB_APP_SECRET` is not set. The command exits with code 2. |
| `Schwab login failed: <ExceptionClassName>` | The login did not finish. The command exits with code 1. Check the callback URL first. |

Hosted alternative: [Nyria](https://nyria.io/automate/tradingview-to-schwab?utm_source=github&utm_medium=docs&utm_campaign=tradingview-webhook-trading-bot) turns TradingView alerts into orders in your own brokerage account, with a free paper account to test first.
