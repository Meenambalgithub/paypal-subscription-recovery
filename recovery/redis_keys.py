"""Every Redis key the system uses, built in one place (docs/LLD.md section 4)."""
import secrets
import time
from contextlib import contextmanager

DEDUPE_TTL_S = 7 * 24 * 3600
LOCK_TTL_MS = 30_000
SCHEDULE_FOLLOWUPS = "schedule:followups"     # sorted set: member = case_id, score = due unix time


def dedupe_event(event_id: str) -> str:
    return f"dedupe:event:{event_id}"


def lock_sub(subscription_id: str) -> str:
    return f"lock:sub:{subscription_id}"


def ratelimit_msg(subscription_id: str) -> str:
    return f"ratelimit:msg:{subscription_id}"


def state_sub(subscription_id: str) -> str:
    return f"state:sub:{subscription_id}"


# Delete the lock only if it still holds our token, so a slow worker whose lock
# expired can't delete the lock another worker now holds.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


class LockTimeout(RuntimeError):
    pass


@contextmanager
def sub_lock(r, subscription_id: str, wait_s: float = 40.0):
    """Hold lock:sub:{id} for the duration of the block.

    wait_s is longer than the lock TTL, so a crashed holder can never block us forever.
    """
    key, token = lock_sub(subscription_id), secrets.token_hex(16)
    deadline = time.monotonic() + wait_s
    while not r.set(key, token, nx=True, px=LOCK_TTL_MS):
        if time.monotonic() > deadline:
            raise LockTimeout(f"could not lock {subscription_id}")
        time.sleep(0.2)
    try:
        yield token
    finally:
        r.eval(_RELEASE, 1, key, token)
