"""The merchant's rules (table merchant_policy) plus fixed safety limits."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config


@dataclass(frozen=True)
class Policy:
    max_followups: int = 2
    quiet_start: int = 21            # customer local hour
    quiet_end: int = 8
    max_pause_days: int = 30         # Week 4
    downgrade_needs_approval: bool = False   # Week 4
    quiet_enabled: bool = True
    tz: str = "Asia/Kolkata"
    # Fixed limits, not merchant-configurable
    min_followup_hours: int = 12
    max_followup_hours: int = 168
    message_gap_hours: int = 24      # at most one message per customer per 24 h


def load(cur) -> Policy:
    cur.execute("SELECT * FROM merchant_policy WHERE id = 1")
    row = cur.fetchone() or {}
    return Policy(
        max_followups=row.get("max_followups", 2),
        quiet_start=row.get("quiet_hours_start", 21),
        quiet_end=row.get("quiet_hours_end", 8),
        max_pause_days=row.get("max_pause_days", 30),
        downgrade_needs_approval=row.get("downgrade_needs_approval", False),
        quiet_enabled=config.QUIET_HOURS != "off",
        tz=config.CUSTOMER_TZ,
    )


def quiet_until(policy: Policy, now: datetime) -> datetime | None:
    """If `now` is inside quiet hours, the moment they end (aware datetime). Else None."""
    if not policy.quiet_enabled:
        return None
    local = now.astimezone(ZoneInfo(policy.tz))
    h, start, end = local.hour, policy.quiet_start, policy.quiet_end
    inside = (start <= h or h < end) if start > end else (start <= h < end)
    if not inside:
        return None
    end_today = local.replace(hour=end, minute=0, second=0, microsecond=0)
    return end_today if end_today > local else end_today + timedelta(days=1)
