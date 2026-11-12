# Week 1 findings: what the real payloads showed

Source: `webhook_events.log`, 9 to 12 Oct 2026 (copy in `tests/fixtures/webhook_events_sample.log`).
Seven events: CREATED, ACTIVATED, three PAYMENT.SALE.COMPLETED (real sandbox), plus one
PAYMENT.FAILED and one SUSPENDED sent from the dashboard's Webhooks simulator.

## 1. Where is the subscription ID?

| Event type | Field | Seen in log |
|---|---|---|
| `BILLING.SUBSCRIPTION.*` | `resource.id` | `I-B1CG95HW6EPG` in CREATED and ACTIVATED |
| `PAYMENT.SALE.*` | `resource.billing_agreement_id` | `resource.id` is the **sale** id (`0L209304CU188050F`), not the subscription |

The LLD guess was right. The webhook is registered for `*`, so other event types
(catalog, plans) also arrive; they have no subscription ID and are acknowledged and dropped.

## 2. Does the payment-failed event include a reason?

Yes: `resource.billing_info.last_failed_payment.reason_code`, next to `amount`, `time`
and the next retry. Also useful: `billing_info.failed_payments_count` and `outstanding_balance`.

Two corrections to the design:

- **The reason codes are coarse.** The API allows only eight values: `PAYMENT_DENIED`,
  `INTERNAL_SERVER_ERROR`, `PAYEE_ACCOUNT_RESTRICTED`, `PAYER_ACCOUNT_RESTRICTED`,
  `PAYER_CANNOT_PAY`, `SENDING_LIMIT_EXCEEDED`, `TRANSACTION_RECEIVING_LIMIT_EXCEEDED`,
  `CURRENCY_MISMATCH`. Codes in the first LLD such as `CARD_EXPIRED` never appear here.
- **`PAYMENT_DENIED` is not a risk code.** PayPal documents it as declined because of
  customer issues, so it's `CUSTOMER_ACTION`. Treating it as RISK would have sent the most
  common failure to the merchant and the agent would almost never talk to customers.
  Three codes are the merchant's own problem, so there's a new `MERCHANT` category.

The only failure sample is from the simulator and is old (2019 dates, live `api.paypal.com`
links). It spells the retry field `next_payment_retry_date`; the API reference says
`next_payment_retry_time`. The code reads both. **A real failure event is still unseen**,
so the processor also fetches the subscription from PayPal when a real failure has no reason.

## 3. What does a renewal look like?

A plain `PAYMENT.SALE.COMPLETED`, identical in shape to the first payment. There is no
separate "renewed" event. Amount is `resource.amount.total` ("20.00").

PayPal's own event list (`GET /v1/notifications/webhooks-events`) shows **both
subscriptions renewed every day**. Four of those renewals never reached the log:

| Renewal sent by PayPal (UTC) | Subscription | In log? |
|---|---|---|
| 10 Oct 12:02 | I-3LGMBKSJRD4X | yes |
| 10 Oct 14:47 | I-B1CG95HW6EPG | **no** |
| 11 Oct 13:41 | I-3LGMBKSJRD4X | **no** |
| 11 Oct 16:07 | I-B1CG95HW6EPG | **no** |
| 12 Oct 11:43 | I-B1CG95HW6EPG | **no** |
| 12 Oct 14:13 | I-3LGMBKSJRD4X | yes |

Cause: the ngrok tunnel was down (turned off on 11 Oct, and not running at the other
times). It was not a sandbox problem. Two lessons:

- **Webhooks get missed, so the system must catch up.** `python check_events.py --resend`
  finds events PayPal sent that aren't in the log and asks PayPal to send them again.
  Resending is safe because the processor dedupes by event ID.
- **Sandbox renewals don't happen at `next_billing_time`.** `I-B1CG95HW6EPG` was due
  10 Oct 10:00 UTC and renewed at 14:47, and the daily times drift by hours. A live
  renewal can't be timed for the demo: record one in advance or use the simulator.

No `BILLING.SUBSCRIPTION.PAYMENT.FAILED` appears in PayPal's list, confirming there
have been no real failures. Webhooks-simulator samples don't appear in the list either.

## 4. Can sandbox negative testing make a subscription payment fail?

No. The Subscriptions test values (`ERRSUB0xx`) only make **your own API calls** return
errors, for example `/v1/billing/subscriptions/ERRSUB071/capture` returns
`INTERNAL_SERVER_ERROR` and `ERRSUB059/suspend` does the same for suspend. None of them
makes a renewal fail or produces `BILLING.SUBSCRIPTION.PAYMENT.FAILED`.

So failure events come from:
1. the dashboard Webhooks simulator (fails signature verification by design, so replay
   it with `replay_events.py`, labelled `source=simulator`);
2. our own labelled simulator in Week 4.

The `ERRSUB` values are still useful in Week 3 to test how the agent's tools handle
PayPal errors on suspend, revise and capture.

## Other things learned

- Simulator samples are easy to spot: 2016/2019 dates, `api.paypal.com` links, fake IDs.
  The SUSPENDED sample uses the old v1 billing-agreement shape (`resource.state`,
  `agreement_details`).
- Events can arrive late or out of order. The processor ignores a failure older than the
  last successful payment, and an older event never overwrites a newer status.
