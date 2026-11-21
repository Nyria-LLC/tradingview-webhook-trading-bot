# How to send TradingView alerts to Alpaca

To send TradingView alerts to Alpaca, point the alert's webhook at this bot and give the bot your Alpaca API keys. The bot reads the alert's JSON and posts a stock or ETF order to Alpaca's `/v2/orders` endpoint. Orders go to Alpaca's paper endpoint until you set `ALPACA_LIVE=true`.

## Prerequisites

- The bot installed and answering the dry-run test in the [quick start](../README.md#quick-start).
- A TradingView plan with webhooks, Essential or higher, and two-factor authentication on.
- An Alpaca account and the API keys for its paper account.

## Credentials

Alpaca issues separate keys for paper and live trading. Its docs say "you cannot use your live account's credentials for paper trading, or vice versa". Start with the paper keys.

Alpaca's [paper trading page](https://docs.alpaca.markets/us/docs/paper-trading) gives two steps for a fresh paper account:

> To create a new paper account, click the paper account number in the upper left corner of the dashboard and select "Open New Paper Account."

> Don't forget to generate new API keys for any newly created account.

## The .env lines

```
DEFAULT_BROKER=alpaca
ALPACA_API_KEY_ID=your-alpaca-key-id
ALPACA_API_SECRET_KEY=your-alpaca-secret-key
ALPACA_LIVE=false
```

`DEFAULT_BROKER` picks the broker for alerts that have no `broker` field. The bot reads `.env` once at startup, so restart it after a change. [Configuration](configuration.md) describes every variable.

## Dry-run test

With `DRY_RUN=true` the bot shows the Alpaca request and sends nothing, so this step needs no keys. Replace `...` with your `WEBHOOK_SECRET`:

```sh
curl -X POST http://127.0.0.1:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"secret": "...", "broker": "alpaca", "symbol": "AAPL", "action": "buy", "quantity": 1}'
```

The bot answers 200:

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

## Alpaca paper trading

Set `DRY_RUN=false`, keep `ALPACA_LIVE=false`, and restart. The same curl command now places a paper order at `https://paper-api.alpaca.markets`:

```json
{
  "ok": true,
  "dry_run": false,
  "broker": "alpaca",
  "order_id": "<Alpaca order id>",
  "client_id": "tvbot-5f0c2a9b7d3e4a1c8b6f2d0e",
  "quantity": "1"
}
```

A 200 means Alpaca accepted the order request. It is not a fill. Look the order up in your paper account by `order_id`, or by `client_id`, which the bot sends as Alpaca's `client_order_id`.

When curl works, point TradingView at the bot. TradingView has to reach it over the internet, so follow [Running it on the internet](../README.md#running-it-on-the-internet) first. [The alert message](../README.md#the-alert-message) has the JSON to paste, and both examples already name `alpaca`.

## Going live

A live order needs two changes: your live keys in `.env`, and `ALPACA_LIVE=true`. Paper keys do not work on the live endpoint. After a restart the bot posts to `https://api.alpaca.markets`, and its startup log line names `alpaca` after `live=`.

Set a small `MAX_QTY` and an `ALLOWED_SYMBOLS` list before the first live alert.

## Alpaca notes

- Fractional quantities work only on market day orders. Alpaca's [order reference](https://docs.alpaca.markets/us/reference/postorder) says `qty` "Can be fractionable for only market and day order types". The bot answers 422 for a fractional limit or `gtc` order.
- The bot reads your positions before every sell and sells only shares you hold long. Alpaca's [order guide](https://docs.alpaca.markets/us/docs/working-with-orders) says "Short orders can also be placed for securities which you do not hold an open long position in", so the check runs before a sell reaches Alpaca.
- The check uses Alpaca's `qty_available` field, "Total number of shares available minus open orders". If open orders already commit every share you hold, the bot refuses the sell.
- The bot sends `day` or `gtc`. Alpaca says a day order "is only valid during Regular Trading Hours (9:30am - 4:00pm ET)" by default, and a gtc order "is good until canceled".
- A limit price goes to Alpaca as the alert sent it. One refusal recorded in Alpaca's SDK tests reads "invalid limit_price 1.0001. sub-penny increment does not fulfill minimum pricing criteria". Round the price in your alert.
- A buy is always sent, even if you hold a short placed outside the bot.

## What has been tested

- Alpaca: requests follow Alpaca's API reference and the recorded responses in Alpaca's own SDK tests. A read-only request to an Alpaca paper account's positions endpoint succeeded.

Every broker request is checked by tests with mocked responses. No order has been placed through this bot by its authors. Start with DRY_RUN=true, then a paper or sandbox account.

## Troubleshooting

| Status | Error | What to do |
|---|---|---|
| 401 | `unauthorized` | Make the alert's `secret` equal `WEBHOOK_SECRET`. |
| 422 | `<broker> is not configured: set <VAR>, <VAR>` | `DRY_RUN` is false and a key is missing. The message names it: `ALPACA_API_KEY_ID` or `ALPACA_API_SECRET_KEY`. Add it to `.env` and restart. |
| 422 | `Alpaca accepts fractional quantities only for market day orders` | Send a whole quantity, or a market order with `day`. |
| 422 | `no long position in <symbol> to sell; this bot does not open short positions` | You hold no long shares of that symbol. A reversing strategy can cause this; see [The alert message](../README.md#the-alert-message). |
| 422 | `sell quantity <quantity> exceeds the <held> shares held in <symbol>` | Lower the quantity, or set `CLAMP_SELLS=true` to sell what you hold. |
| 422 | `the shares held in <symbol> are committed to open orders; none are available to sell` | Cancel the open order that holds the shares, then send the alert again. |
| 424 | `"broker_status": 403` | Alpaca refused the order. Its reference describes 403 as "Buying power or shares is not sufficient." The error text carries Alpaca's message, such as "insufficient buying power". |
| 424 | `"broker_status": 422` | Alpaca refused the order. Its reference describes 422 as "Input parameters are not recognized." Check the symbol and the limit price. |
| 424 | `"broker_status": null` | The bot could not reach Alpaca, or a position read failed in transit. No order was sent. Send the alert again. |
| 504 | `outcome unknown (...)` | The order may have been placed. Look for the client id in your Alpaca orders before you resend. |

If Alpaca refuses your keys, check the pairing. Paper keys work only while `ALPACA_LIVE=false`, and live keys only once it is `true`.

Hosted alternative: [Nyria](https://nyria.io/automate/tradingview-to-alpaca?utm_source=github&utm_medium=docs&utm_campaign=tradingview-webhook-trading-bot) turns TradingView alerts into orders in your own brokerage account, with a free paper account to test first.
