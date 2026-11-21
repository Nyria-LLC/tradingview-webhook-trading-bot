# How to send TradingView alerts to Tradier

To send TradingView alerts to Tradier, point the alert's webhook at this bot and give the bot a Tradier access token and account id. The bot reads the alert's JSON and posts an equity order to Tradier's `/v1/accounts/{account_id}/orders` endpoint. Orders go to Tradier's sandbox host until you set `TRADIER_LIVE=true`.

## Prerequisites

- The bot installed and answering the dry-run test in the [quick start](../README.md#quick-start).
- A TradingView plan with webhooks, Essential or higher, and two-factor authentication on.
- A Tradier account, its access token and its account id.

## Credentials

The bot needs two values: an access token, and the id of the account it trades. Use the access token for the Tradier account the bot points at. While `TRADIER_LIVE=false`, that is your paper trading account on `https://sandbox.tradier.com`.

Tradier's documentation is at [docs.tradier.com](https://docs.tradier.com/).

## The .env lines

```
DEFAULT_BROKER=tradier
TRADIER_ACCESS_TOKEN=your-tradier-access-token
TRADIER_ACCOUNT_ID=your-tradier-account-id
TRADIER_LIVE=false
```

`DEFAULT_BROKER` picks the broker for alerts that have no `broker` field. The bot reads `.env` once at startup, so restart it after a change. [Configuration](configuration.md) describes every variable.

## Dry-run test

With `DRY_RUN=true` the bot shows the Tradier request and sends nothing, so this step needs no token. Replace `...` with your `WEBHOOK_SECRET`:

```sh
curl -X POST http://127.0.0.1:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"secret": "...", "broker": "tradier", "symbol": "AAPL", "action": "buy", "quantity": 1}'
```

The bot answers 200:

```json
{
  "ok": true,
  "dry_run": true,
  "broker": "tradier",
  "request": {
    "method": "POST",
    "url": "https://sandbox.tradier.com/v1/accounts/<TRADIER_ACCOUNT_ID>/orders",
    "form": {
      "class": "equity",
      "symbol": "AAPL",
      "side": "buy",
      "quantity": "1",
      "type": "market",
      "duration": "day",
      "tag": "tvbot-5f0c2a9b7d3e4a1c8b6f2d0e"
    }
  }
}
```

`<TRADIER_ACCOUNT_ID>` is a placeholder. A dry run never shows your real account id.

## Tradier sandbox

Set `DRY_RUN=false`, keep `TRADIER_LIVE=false`, and restart. The bot now posts to `https://sandbox.tradier.com/v1`. Tradier's docs describe that host this way: "If you're developing using a paper trading account, change the hostname to https://sandbox.tradier.com". The bot makes that switch for you.

The same curl command now answers with an order id:

```json
{
  "ok": true,
  "dry_run": false,
  "broker": "tradier",
  "order_id": "<Tradier order id>",
  "client_id": "tvbot-5f0c2a9b7d3e4a1c8b6f2d0e",
  "quantity": "1"
}
```

A 200 means Tradier accepted the order request. It is not a fill. The bot sends `client_id` to Tradier as the order's `tag`.

When curl works, point TradingView at the bot. TradingView has to reach it over the internet, so follow [Running it on the internet](../README.md#running-it-on-the-internet) first. [The alert message](../README.md#the-alert-message) has the JSON to paste. Change `"broker"` to `"tradier"`, or set `DEFAULT_BROKER=tradier` and drop the field.

## Going live

A live order needs `TRADIER_LIVE=true`, plus the token and account id of your live account. After a restart the bot posts to `https://api.tradier.com/v1`, and its startup log line names `tradier` after `live=`.

Set a small `MAX_QTY` and an `ALLOWED_SYMBOLS` list before the first live alert.

## Tradier notes

- Orders are whole shares. Tradier's trading reference defines quantity as "The number of shares to be ordered, in whole numbers." The bot answers 422 for a fractional quantity.
- The bot reads your positions before every sell and sells only shares you hold long. Tradier reports a position's quantity with a sign: "positive numbers indicate long positions, negative numbers indicate short positions".
- The position check does not subtract open sell orders. If a sell for those shares is already open at Tradier, a new sell still passes the check.
- Tradier can put an error inside an HTTP 200 reply. Its errors page says "the API sends back a 200 response for any error that is thrown by downstream systems", and that the body "will include an errors property". The bot reads that property and answers 424.
- A buy is always sent, even if you hold a short placed outside the bot.

## What has been tested

- Tradier: requests follow Tradier's API reference.

Every broker request is checked by tests with mocked responses. No order has been placed through this bot by its authors. Start with DRY_RUN=true, then a paper or sandbox account.

## Troubleshooting

| Status | Error | What to do |
|---|---|---|
| 401 | `unauthorized` | Make the alert's `secret` equal `WEBHOOK_SECRET`. |
| 422 | `<broker> is not configured: set <VAR>, <VAR>` | `DRY_RUN` is false and a value is missing. The message names it: `TRADIER_ACCESS_TOKEN` or `TRADIER_ACCOUNT_ID`. Add it to `.env` and restart. |
| 422 | `Tradier orders must be whole shares` | Send a whole quantity. |
| 422 | `no long position in <symbol> to sell; this bot does not open short positions` | You hold no long shares of that symbol. A reversing strategy can cause this; see [The alert message](../README.md#the-alert-message). |
| 422 | `sell quantity <quantity> exceeds the <held> shares held in <symbol>` | Lower the quantity, or set `CLAMP_SELLS=true` to sell what you hold. |
| 424 | `"broker_status": 401` | Tradier describes 401 as "Authentication required or invalid credentials". Check the token belongs to the account the bot points at. |
| 424 | `"broker_status": 403` or `404` | Tradier describes these as "Access denied to the requested resource" and "The requested resource was not found". Check `TRADIER_ACCOUNT_ID` and the token. |
| 424 | `"broker_status": 400` | Tradier describes 400 as "Invalid request parameters or body". Check the symbol and the limit price. |
| 424 | `"broker_status": 200` | Tradier's reply says the order was not accepted. The error text carries Tradier's reason. |
| 424 | `"broker_status": null` | The bot could not reach Tradier, or a position read failed in transit. No order was sent. Send the alert again. |
| 504 | `outcome unknown (...)` | The order may have been placed. The client id in the message is the order's `tag`. Check your Tradier orders before you resend. |

Hosted alternative: [Nyria](https://nyria.io/automate/tradingview-to-tradier?utm_source=github&utm_medium=docs&utm_campaign=tradingview-webhook-trading-bot) turns TradingView alerts into orders in your own brokerage account, with a free paper account to test first.
