import pytest

from recovery.classifier import (CUSTOMER_ACTION, MERCHANT, PAST_DUE, RISK, TEMPORARY, UNKNOWN, classify)


@pytest.mark.parametrize("code, category", [
    ("PAYMENT_DENIED", CUSTOMER_ACTION),
    ("PAYER_CANNOT_PAY", CUSTOMER_ACTION),
    ("SENDING_LIMIT_EXCEEDED", CUSTOMER_ACTION),
    ("INTERNAL_SERVER_ERROR", TEMPORARY),
    ("PAYER_ACCOUNT_RESTRICTED", RISK),
    ("PAYEE_ACCOUNT_RESTRICTED", MERCHANT),
    ("TRANSACTION_RECEIVING_LIMIT_EXCEEDED", MERCHANT),
    ("CURRENCY_MISMATCH", MERCHANT),
])
def test_every_paypal_reason_code_is_mapped(code, category):
    assert classify(code, 1).category == category


def test_no_reason_waits_for_one_paypal_retry():
    assert classify(None, 1).category == UNKNOWN
    assert classify(None, 2).category == CUSTOMER_ACTION


def test_suspended_is_past_due_unless_risk_or_merchant():
    assert classify("PAYMENT_DENIED", 3, suspended=True).category == PAST_DUE
    assert classify("PAYER_ACCOUNT_RESTRICTED", 3, suspended=True).category == RISK
    assert classify("CURRENCY_MISMATCH", 3, suspended=True).category == MERCHANT


def test_risk_and_merchant_never_contact_customer():
    assert not classify("PAYER_ACCOUNT_RESTRICTED").contact_customer
    assert not classify("PAYEE_ACCOUNT_RESTRICTED").contact_customer
    assert classify("PAYMENT_DENIED").contact_customer


def test_case_and_whitespace_tolerant():
    assert classify(" payment_denied ").category == CUSTOMER_ACTION
