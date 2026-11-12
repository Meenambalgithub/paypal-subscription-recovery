"""Shared fixtures.

Unit tests need nothing running. Integration tests (marked `integration`) need the
Docker Postgres and Redis from infra/docker-compose.yml. They use a separate
database (recovery_test) and Redis db 15, so your dev data is never touched.
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PAYPAL_CLIENT_ID", "test-client-id")      # app.py needs these to import
os.environ.setdefault("PAYPAL_CLIENT_SECRET", "test-secret")

SAMPLE_LOG = ROOT / "tests" / "fixtures" / "webhook_events_sample.log"


@pytest.fixture(scope="session")
def sample_events():
    """Raw PayPal events from the real Week 1 webhook_events.log."""
    return [json.loads(line)["raw"] for line in SAMPLE_LOG.read_text().splitlines() if line.strip()]


def event_of(sample_events, event_type, index=0):
    return [e for e in sample_events if e["event_type"] == event_type][index]


# ---------------------------------------------------------------- integration

def _test_db_url():
    from recovery import config
    base = os.getenv("TEST_DATABASE_URL") or config.DATABASE_URL
    return base.rsplit("/", 1)[0] + "/recovery_test"


@pytest.fixture(scope="session")
def pg_url():
    psycopg = pytest.importorskip("psycopg")
    from recovery import config
    admin = (os.getenv("TEST_DATABASE_URL") or config.DATABASE_URL)
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as c:
            if not c.execute("SELECT 1 FROM pg_database WHERE datname = 'recovery_test'").fetchone():
                c.execute("CREATE DATABASE recovery_test")
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not reachable ({e}). Start infra/docker-compose.yml.")
    url = _test_db_url()
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute((ROOT / "infra" / "schema.sql").read_text())
        for m in sorted((ROOT / "infra" / "migrations").glob("*.sql")):
            c.execute(m.read_text())
    return url


@pytest.fixture
def conn(pg_url):
    from recovery import db
    c = db.connect(pg_url)
    c.execute("TRUNCATE approvals, agent_actions, messages, recovery_cases, subscription_events, subscribers "
              "RESTART IDENTITY CASCADE")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def r():
    redis = pytest.importorskip("redis")
    from recovery import config
    url = os.getenv("TEST_REDIS_URL") or config.REDIS_URL.rsplit("/", 1)[0] + "/15"
    client = redis.from_url(url, decode_responses=True)
    try:
        client.ping()
    except redis.ConnectionError as e:
        pytest.skip(f"Redis not reachable ({e}). Start infra/docker-compose.yml.")
    client.flushdb()
    yield client
    client.flushdb()
