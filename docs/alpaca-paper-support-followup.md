# Alpaca support follow-up — prepared, unsent

Status checked September 11, 2026. No follow-up was sent during this check.

Ticket **353754** has two inbound notifications in a separate forwarded-request
thread: receipt acknowledged at **16:28:52 UTC / 9:28:52 AM PDT**, followed by
a response at **16:32:46 UTC / 9:32:46 AM PDT**. The second message explicitly
states that it was generated using AI. It mentions wash-trade protection or
account restrictions as common possibilities and asks for the complete error,
order details and environment. Those suggestions are not a diagnosis of the
recorded request. No account correction or successful order is established.

The reply states that tickets automatically close after that message and that
replying will bring the request to a team member. Its actual ticket state was
not separately inspected. The discarded error-message body and missing HTTP
trace ID cannot be recovered from the retained local evidence. The two-attempt
pilot remains halted; the follow-up does not require another submission.

[Read the support reply in Gmail](https://mail.google.com/mail/#all/1a0915080e7a484a).
The original sent request and its evidence are documented in
[alpaca-paper-support-draft.md](alpaca-paper-support-draft.md).

## Ready-to-send reply

Subject: Re: [AlpacaDB, Inc.] Re: Fwd: Paper BTC/USD limit order rejected with 40310000 despite active account and $100 buying power

Hello Alpaca Support,

Please have a team member review ticket 353754 and check the actual rejection
in your logs. The automated response lists possible causes but does not identify
which check rejected this request.

This was my **ATLAS $100 Paper** account in the paper environment, using a
paper-account API key. At approximately **16:33:31 UTC on September 10, 2026**,
I sent the following request to
`POST https://paper-api.alpaca.markets/v2/orders`:

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

The API returned **HTTP 403**, code **40310000**, and no broker order ID.
The client retained the status and numeric code but discarded the message body;
it also did not retain an HTTP request trace ID. I cannot recover those missing
fields from the saved evidence.

Authenticated reads immediately before and after the request showed ACTIVE
account and crypto status; $100 cash, equity, buying power and non-marginable
buying power; no account or trading blocks; and no positions, orders or account
activities. BTC/USD was marked tradable. The same paper account's dashboard
showed Trades Suspended off and Fractional Trading on. The order notional was
approximately $1.988. These are observations from that test, not a new account
snapshot.

Please locate the refusal using the timestamp, paper account and client order
ID, identify the specific validation or account restriction that caused it,
and explain the required correction. If you need another identifier, please
provide an authenticated support route for supplying it. API credentials are
not included in this email.

The pilot is halted after its two rejected attempts. No broker order was found,
no cancellation was sent, and no further submission is planned while the
rejection remains unexplained.

Thank you.
