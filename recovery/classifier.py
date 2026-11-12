"""Failure reason -> category -> playbook (docs/LLD.md section 6, revised after Week 1).

billing_info.last_failed_payment.reason_code only takes these values
(PayPal Subscriptions API, failed_payment_details):

    PAYMENT_DENIED, INTERNAL_SERVER_ERROR, PAYEE_ACCOUNT_RESTRICTED,
    PAYER_ACCOUNT_RESTRICTED, PAYER_CANNOT_PAY, SENDING_LIMIT_EXCEEDED,
    TRANSACTION_RECEIVING_LIMIT_EXCEEDED, CURRENCY_MISMATCH

The first LLD draft used codes like CARD_EXPIRED that never appear in this field.
PAYMENT_DENIED is documented as "declined due to one or more customer issues",
so it is CUSTOMER_ACTION, not RISK. Three codes are the merchant's problem, not
the customer's, so they get their own category and are never sent to the customer.
"""
from __future__ import annotations

from dataclasses import dataclass

TEMPORARY = "TEMPORARY"
CUSTOMER_ACTION = "CUSTOMER_ACTION"
PAST_DUE = "PAST_DUE"
RISK = "RISK"
MERCHANT = "MERCHANT"
UNKNOWN = "UNKNOWN"

# Categories the customer must never hear about. Code enforces this, not the prompt.
NO_CUSTOMER_CONTACT = {RISK, MERCHANT}

_BY_REASON = {
    "PAYMENT_DENIED": (CUSTOMER_ACTION, "Ask them to check their PayPal balance or update the card or bank linked to this subscription."),
    "PAYER_CANNOT_PAY": (CUSTOMER_ACTION, "Ask them to add or choose a different payment method in PayPal."),
    "SENDING_LIMIT_EXCEEDED": (CUSTOMER_ACTION, "Their PayPal sending limit was reached. They can confirm their account details in PayPal to lift it, or pay another way."),
    "INTERNAL_SERVER_ERROR": (TEMPORARY, "A PayPal-side error. PayPal will retry automatically; no action needed."),
    "PAYER_ACCOUNT_RESTRICTED": (RISK, "The customer's PayPal account is restricted. Merchant review only."),
    "PAYEE_ACCOUNT_RESTRICTED": (MERCHANT, "The merchant's PayPal account is restricted. Fix the merchant account."),
    "TRANSACTION_RECEIVING_LIMIT_EXCEEDED": (MERCHANT, "The merchant's receiving limit was reached. Fix the merchant account."),
    "CURRENCY_MISMATCH": (MERCHANT, "The merchant account doesn't accept this currency. Fix the merchant settings."),
}


@dataclass(frozen=True)
class Classification:
    category: str
    reason_code: str | None
    hint: str            # one line for the agent's context in Week 3

    @property
    def contact_customer(self) -> bool:
        return self.category not in NO_CUSTOMER_CONTACT


def classify(reason_code: str | None, failed_payments_count: int | None = None,
             suspended: bool = False) -> Classification:
    code = (reason_code or "").strip().upper() or None

    # Reasons that say whose problem it is win over everything else.
    if code in _BY_REASON and _BY_REASON[code][0] in NO_CUSTOMER_CONTACT:
        category, hint = _BY_REASON[code]
        return Classification(category, code, hint)

    if suspended:
        return Classification(PAST_DUE, code, "PayPal suspended the subscription after repeated failures. "
                                              "They need to settle the balance to restart it.")
    if code in _BY_REASON:
        category, hint = _BY_REASON[code]
        return Classification(category, code, hint)

    # No reason (or one PayPal added later): give PayPal's retry one chance first.
    if (failed_payments_count or 0) >= 2:
        return Classification(CUSTOMER_ACTION, code, "Repeated failures with no clear reason. "
                                                     "Ask them to check the payment method in PayPal.")
    return Classification(UNKNOWN, code, "No reason given. Waiting for PayPal's retry before contacting.")
