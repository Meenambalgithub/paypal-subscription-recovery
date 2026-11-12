"""Recovery case states and the only transitions allowed (docs/LLD.md section 5).

Everything in the LLD diagram, plus three rules the diagram implies but didn't draw
(listed in docs/LLD.md "Changes after Week 1"):
  1. Any open state -> RECOVERED when a payment succeeds (AT_RISK -> CLOSED_OK instead).
  2. Any open state -> ESCALATED when a later failure turns out to be RISK or MERCHANT.
  3. Any open state -> LOST when the subscription is cancelled or expires.
"""
from __future__ import annotations

OPEN = "OPEN"
AT_RISK = "AT_RISK"
WAITING_PAYPAL = "WAITING_PAYPAL"
CONTACTED = "CONTACTED"
NEGOTIATING = "NEGOTIATING"
ESCALATED = "ESCALATED"
RECOVERED = "RECOVERED"
PAUSED = "PAUSED"
DOWNGRADED = "DOWNGRADED"
LOST = "LOST"
CLOSED_OK = "CLOSED_OK"

TERMINAL = {RECOVERED, PAUSED, DOWNGRADED, LOST, CLOSED_OK}
OPEN_STATES = {OPEN, AT_RISK, WAITING_PAYPAL, CONTACTED, NEGOTIATING, ESCALATED}
INITIAL = {OPEN, AT_RISK}

# Straight from the diagram.
_DIAGRAM = {
    AT_RISK: {CLOSED_OK, OPEN},
    OPEN: {WAITING_PAYPAL, CONTACTED, ESCALATED},
    WAITING_PAYPAL: {RECOVERED, CONTACTED},
    CONTACTED: {CONTACTED, NEGOTIATING, RECOVERED, LOST},
    NEGOTIATING: {PAUSED, DOWNGRADED, RECOVERED},
    ESCALATED: {CLOSED_OK},
}

ALLOWED: dict[str, set[str]] = {s: set(_DIAGRAM.get(s, ())) for s in OPEN_STATES | TERMINAL}
for _s in OPEN_STATES:
    if _s != AT_RISK:
        ALLOWED[_s].add(RECOVERED)                 # rule 1
    if _s not in (ESCALATED, AT_RISK):           # AT_RISK goes to OPEN first on a failure
        ALLOWED[_s].add(ESCALATED)                 # rule 2
    ALLOWED[_s].add(LOST)                          # rule 3
ALLOWED[AT_RISK].discard(LOST)                     # nothing was lost: close it OK instead
ALLOWED[AT_RISK].add(CLOSED_OK)


class InvalidTransition(ValueError):
    pass


def can_move(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, set())


def check(current: str, target: str) -> str:
    if not can_move(current, target):
        raise InvalidTransition(f"{current} -> {target} is not allowed")
    return target


def on_payment_succeeded(current: str) -> str:
    return CLOSED_OK if current == AT_RISK else RECOVERED


def on_subscription_ended(current: str) -> str:
    return CLOSED_OK if current == AT_RISK else LOST


def outcome_for(state: str) -> str | None:
    """recovery_cases.outcome for a terminal state."""
    return state if state in TERMINAL else None
