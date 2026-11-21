# How to send TradingView alerts to tastytrade

To send TradingView alerts to tastytrade, point the alert's webhook at this bot and give the bot a tastytrade OAuth client secret, a refresh token and your account number. The bot reads the alert's JSON and posts a one-leg equity order to tastytrade's `/accounts/{account_number}/orders` endpoint. Orders go to tastytrade's sandbox host until you set `TASTYTRADE_LIVE=true`.

## Prerequisites

- The bot installed and answering the dry-run test in the [quick start](../README.md#quick-start).
- A TradingView plan with webhooks, Essential or higher, and two-factor authentication on.
- A tastytrade account. The first run uses sandbox credentials, which are separate from production ones.

## Credentials

The bot signs in with a personal OAuth application. tastytrade's [OAuth2 guide](https://developer.tastytrade.com/docs/authentication/oauth2/) gives the steps:

1. On my.tastytrade.com, go to Manage, My Profile, API, OAuth Applications, and click "+ New OAuth client".
2. Give it the scopes `read`, `trade` and `openid`.
3. Copy the client secret. tastytrade shows it once.
4. Back under OAuth Applications, click Manage next to your application, then Create Grant. This shows your grant's refresh token.

Three facts from the same guide shape the setup:

- Refresh tokens "are long-lived and do not expire". You paste one into `.env` once.
- Access tokens last 15 minutes. The bot requests a new one when it needs it.
- Sandbox and production have separate credentials. A client and refresh token registered in one environment "will not work in the other".

tastytrade's [sandbox page](https://developer.tastytrade.com/docs/sandbox/) covers the sandbox itself.

## The .env lines

```
DEFAULT_BROKER=tastytrade
TASTYTRADE_CLIENT_SECRET=your-tastytrade-client-secret
TASTYTRADE_REFRESH_TOKEN=your-tastytrade-refresh-token
TASTYTRADE_ACCOUNT_NUMBER=your-tastytrade-account-number
TASTYTRADE_LIVE=false
```

`DEFAULT_BROKER` picks the broker for alerts that have no `broker` field. The bot reads `.env` once at startup, so restart it after a change. [Configuration](configuration.md) describes every variable.

## Dry-run test

With `DRY_RUN=true` the bot shows the tastytrade request and sends nothing. This step needs no credentials, and it fetches no token. Replace `...` with your `WEBHOOK_SECRET`:

```sh
curl -X POST http://127.0.0.1:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"secret": "...", "broker": "tastytrade", "symbol": "AAPL", "action": "buy", "quantity": 1}'
```

The bot answers 200:

```json
{
  "ok": true,
  "dry_run": true,
  "broker": "tastytrade",
  "request": {
    "method": "POST",
    "url": "https://api.cert.tastyworks.com/accounts/<TASTYTRADE_ACCOUNT_NUMBER>/orders",
    "json": {
      "time-in-force": "Day",
      "order-type": "Market",
      "source": "tradingview-webhook-trading-bot",
      "external-identifier": "tvbot-5f0c2a9b7d3e4a1c8b6f2d0e",
      "legs": [
        {
          "instrument-type": "Equity",
          "symbol": "AAPL",
          "quantity": "1",
          "action": "Buy to Open"
        }
      ]
    }
  }
}
```

`<TASTYTRADE_ACCOUNT_NUMBER>` is a placeholder. A dry run never shows your real account number.

## tastytrade sandbox

Set `DRY_RUN=false`, keep `TASTYTRADE_LIVE=false`, and restart. The bot now posts to `https://api.cert.tastyworks.com`, with the client secret, refresh token and account number of your sandbox account.

tastytrade describes the sandbox as "a full copy of the tastytrade API backed by simulated money", and says orders placed there "never reach a real market".

The same curl command now answers with an order id:

```json
{
  "ok": true,
  "dry_run": false,
  "broker": "tastytrade",
  "order_id": "<tastytrade order id>",
  "client_id": "tvbot-5f0c2a9b7d3e4a1c8b6f2d0e",
  "quantity": "1"
}
```

A 200 means tastytrade accepted the order request. It is not a fill. The bot sends `client_id` to tastytrade as the order's `external-identifier`.

When curl works, point TradingView at the bot. TradingView has to reach it over the internet, so follow [Running it on the internet](../README.md#running-it-on-the-internet) first. [The alert message](../README.md#the-alert-message) has the JSON to paste. Change `"broker"` to `"tastytrade"`, or set `DEFAULT_BROKER=tastytrade` and drop the field.

## Going live

A live order needs `TASTYTRADE_LIVE=true`, plus the client secret, refresh token and account number of your production account. Sandbox credentials do not work there. After a restart the bot posts to `https://api.tastyworks.com`, and its startup log line names `tastytrade` after `live=`.

Set a small `MAX_QTY` and an `ALLOWED_SYMBOLS` list before the first live alert.

## tastytrade notes

- Orders are whole shares in this bot. It answers 422 for a fractional quantity.
- Market orders are day orders. tastytrade's order rules list a Market order's time in force as "not `GTC`", so the bot answers 422 for a market order with `gtc`. Use a limit order for `gtc`.
- A buy goes out as "Buy to Open" and a sell as "Sell to Close". Limit orders add the price as a Debit for a buy and a Credit for a sell.
- The bot reads your positions before every sell and sells only shares you hold long. The check does not subtract open sell orders. If a sell for those shares is already open at tastytrade, a new sell still passes the check.
- The first order after a start makes up to two extra calls: a token request, and a position read before a sell. The bot then reuses the access token until shortly before it expires.
- The bot never resends an order after a timeout or a 5xx. tastytrade's docs say "the API does not deduplicate retried submissions", so the bot answers 504 and leaves the check to you.
- A buy is always sent, even if you hold a short placed outside the bot.

## What has been tested

- tastytrade: requests follow tastytrade's API reference and OpenAPI schema.

Every broker request is checked by tests with mocked responses. No order has been placed through this bot by its authors. Start with DRY_RUN=true, then a paper or sandbox account.

## Troubleshooting

| Status | Error | What to do |
|---|---|---|
| 401 | `unauthorized` | Make the alert's `secret` equal `WEBHOOK_SECRET`. |
| 422 | `<broker> is not configured: set <VAR>, <VAR>` | `DRY_RUN` is false and a value is missing. The message names it: `TASTYTRADE_CLIENT_SECRET`, `TASTYTRADE_REFRESH_TOKEN` or `TASTYTRADE_ACCOUNT_NUMBER`. Add it to `.env` and restart. |
| 422 | `tastytrade orders must be whole shares in this bot` | Send a whole quantity. |
| 422 | `tastytrade market orders must be day orders` | Send the market order with `day`, or use a limit order with `gtc`. |
| 422 | `no long position in <symbol> to sell; this bot does not open short positions` | You hold no long shares of that symbol. A reversing strategy can cause this; see [The alert message](../README.md#the-alert-message). |
| 422 | `sell quantity <quantity> exceeds the <held> shares held in <symbol>` | Lower the quantity, or set `CLAMP_SELLS=true` to sell what you hold. |
| 424 | `"broker_status": 401` | tastytrade describes 401 as "Missing or expired access token, or a missing/malformed `User-Agent` header." The bot has already fetched a new token and tried once more. Check the client secret and refresh token. |
| 424 | `"broker_status": 403` | tastytrade describes 403 as "The token's scope or the account authority does not permit placing orders on this account." Check the grant has the `trade` scope. |
| 424 | `"broker_status": 404` | tastytrade describes 404 as "The account number does not exist or is not accessible with this token." Check `TASTYTRADE_ACCOUNT_NUMBER`, and that it belongs to the environment the bot points at. |
| 424 | `"broker_status": 422` | tastytrade describes 422 as "The order failed one or more preflight checks (for example `cant_buy_for_credit`, an invalid symbol, or insufficient buying power)." The error text carries tastytrade's reason. |
| 424 | `"broker_status": 429` | tastytrade describes 429 as "Request rate exceeded." Send fewer alerts at once. |
| 424 | `"broker_status": null` | The bot could not reach tastytrade, or a token request or position read failed in transit. No order was sent. Send the alert again. |
| 504 | `outcome unknown (...)` | The order may have been placed. The client id in the message is the order's `external-identifier`. Check your tastytrade orders before you resend. |

If every order fails at the token request, check that the client secret and refresh token come from the same environment as `TASTYTRADE_LIVE`.

Hosted alternative: [Nyria](https://nyria.io/automate/tradingview-to-tastytrade?utm_source=github&utm_medium=docs&utm_campaign=tradingview-webhook-trading-bot) turns TradingView alerts into orders in your own brokerage account, with a free paper account to test first.
