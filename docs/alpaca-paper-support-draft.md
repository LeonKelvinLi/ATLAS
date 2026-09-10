# Alpaca support request — sent September 10, 2026

Sent to `support@alpaca.markets` after Leon continued the prepared request.
Gmail returned the `SENT` label. The external evidence directory contains
`support-sent-receipt.json`; delivery, support acknowledgement and resolution
have not yet been verified. The body below was sent as plain text.

Subject: Paper BTC/USD limit order rejected with 40310000 despite active account and $100 buying power

Hello Alpaca Support,

Please help identify why a small BTC/USD limit buy was rejected on my dedicated
paper account, **ATLAS $100 Paper**, account number ending **XWHX**. This is paper
trading only. The request used `https://paper-api.alpaca.markets/v2/orders` on
September 10, 2026, at approximately **16:33:31 UTC**:

```json
{
  "client_order_id": "0d30d984-5342-5257-8071-99fe14d2ef96",
  "limit_price": "75324.76",
  "qty": "0.000026393",
  "side": "buy",
  "symbol": "BTC/USD",
  "time_in_force": "gtc",
  "type": "limit"
}
```

The response was **HTTP 403**, with numeric provider code **40310000**. My client
retained those fields but discarded the provider message, so I cannot supply the
exact reason text or an HTTP request trace ID. No broker order ID was returned.

Authenticated paper reads immediately before and after the request showed:

- Account status ACTIVE and crypto status ACTIVE.
- Cash, equity, buying power and non-marginable buying power all $100.
- Account blocked, trading blocked and trading suspended by user all false.
- No positions, order history or account activities.
- BTC/USD asset metadata marked tradable.

The order notional was approximately $1.988. In the same paper account's
dashboard, Trades Suspended is off and Fractional Trading is on. Lookup by the
client order ID and order history did not find an order. No cancellation was
sent because there was no order ID to cancel.

One earlier request at approximately 16:15:51 UTC used client order ID
`90ea8396-05dc-5b7f-a6a8-20e5f482d552`, quantity `0.000026365` and limit price
`75403.2874`. It was also rejected, but that older client did not retain the exact
HTTP status or message. I am not assuming both rejections have the same cause.

Can you identify the rejection reason in your logs using the account and client
order ID, and confirm whether a paper-account permission, eligibility check or
specific request constraint needs to be corrected? The test client is halted;
I have not reset the account, added funds or sent further attempts.

Thank you.

---

The sent message contained no API keys or secrets. No additional paper order
was submitted, and the existing pilot journal remains halted.
