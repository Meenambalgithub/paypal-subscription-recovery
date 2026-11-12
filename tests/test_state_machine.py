import pytest

from recovery import state_machine as sm

DIAGRAM_EDGES = [
    ("AT_RISK", "CLOSED_OK"), ("AT_RISK", "OPEN"),
    ("OPEN", "WAITING_PAYPAL"), ("OPEN", "CONTACTED"), ("OPEN", "ESCALATED"),
    ("WAITING_PAYPAL", "RECOVERED"), ("WAITING_PAYPAL", "CONTACTED"),
    ("CONTACTED", "CONTACTED"), ("CONTACTED", "NEGOTIATING"), ("CONTACTED", "RECOVERED"), ("CONTACTED", "LOST"),
    ("NEGOTIATING", "PAUSED"), ("NEGOTIATING", "DOWNGRADED"), ("NEGOTIATING", "RECOVERED"),
    ("ESCALATED", "CLOSED_OK"),
]


@pytest.mark.parametrize("a, b", DIAGRAM_EDGES)
def test_diagram_edges_allowed(a, b):
    assert sm.check(a, b) == b


@pytest.mark.parametrize("a, b", [
    ("OPEN", "PAUSED"), ("WAITING_PAYPAL", "NEGOTIATING"), ("CONTACTED", "OPEN"),
    ("AT_RISK", "CONTACTED"), ("ESCALATED", "CONTACTED"), ("OPEN", "DOWNGRADED"),
])
def test_other_edges_rejected(a, b):
    with pytest.raises(sm.InvalidTransition):
        sm.check(a, b)


@pytest.mark.parametrize("terminal", sorted(sm.TERMINAL))
def test_terminal_states_have_no_exits(terminal):
    assert not sm.ALLOWED[terminal]


@pytest.mark.parametrize("state", sorted(sm.OPEN_STATES))
def test_success_always_closes_an_open_case(state):
    assert sm.can_move(state, sm.on_payment_succeeded(state))


@pytest.mark.parametrize("state", sorted(sm.OPEN_STATES))
def test_cancellation_always_closes_an_open_case(state):
    assert sm.can_move(state, sm.on_subscription_ended(state))
