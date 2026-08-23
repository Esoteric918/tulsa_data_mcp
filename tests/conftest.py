"""Shared pytest fixtures for the CountyLayer test suite.

Import order matters here: DB_NAME is pinned to the test database and the
test DB/schema are bootstrapped BEFORE db.py (or anything that imports it)
is ever imported - db.py opens a connection pool at module import time, so
if we imported it first it would either connect to the wrong database or
fail outright because the test database doesn't exist yet.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEST_DB_NAME = os.environ.get("TEST_DB_NAME", "tulsa_data_test")
os.environ["DB_NAME"] = TEST_DB_NAME

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Hard guard: never let this suite run against the real dev/prod database.
# TRUNCATEs happen between every test (see reset_db below) - pointed at the
# wrong database, this would silently wipe real data.
assert TEST_DB_NAME != "tulsa_data" and "test" in TEST_DB_NAME, (
    f"Refusing to run tests against DB_NAME={TEST_DB_NAME!r} - "
    "set TEST_DB_NAME to something containing 'test'."
)

import psycopg2  # noqa: E402


def _ensure_test_db_exists():
    conn_kwargs = dict(
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
    )
    conn = psycopg2.connect(dbname="postgres", **conn_kwargs)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        conn.close()

    conn = psycopg2.connect(dbname=TEST_DB_NAME, **conn_kwargs)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.api_keys')")
            has_schema = cur.fetchone()[0] is not None
        if not has_schema:
            schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
            with open(schema_path) as f:
                schema_sql = f.read()
            with conn.cursor() as cur:
                cur.execute(schema_sql)
            conn.commit()
    finally:
        conn.close()


_ensure_test_db_exists()

import stripe  # noqa: E402

assert os.environ["STRIPE_SECRET_KEY"].startswith("sk_test_"), (
    "Refusing to run tests against a non-test-mode Stripe key"
)
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

import pytest  # noqa: E402
from unittest.mock import patch, MagicMock  # noqa: E402

from db import get_cursor  # noqa: E402

# Real Stripe test-mode Prices for this project (see billing.py's
# _price_metadata - tier/quota/rate-limit metadata lives on these).
MANUAL_PRICE = os.environ.get("TEST_MANUAL_PRICE_ID", "price_1U4vG0EdXSkCTnpVNfeKKfKN")
AGENT_PRICE = os.environ.get("TEST_AGENT_PRICE_ID", "price_1U4vKjEdXSkCTnpVUrtSNrM9")

_RESET_TABLES = ["usage_log", "api_keys", "stripe_events", "customer_cooldowns"]


@pytest.fixture(autouse=True)
def reset_db():
    """Every test starts and ends with these tables empty, so no test can
    see another test's rows and order never matters."""
    with get_cursor(commit=True) as cur:
        cur.execute(f"TRUNCATE TABLE {', '.join(_RESET_TABLES)} RESTART IDENTITY CASCADE")
    yield
    with get_cursor(commit=True) as cur:
        cur.execute(f"TRUNCATE TABLE {', '.join(_RESET_TABLES)} RESTART IDENTITY CASCADE")


@pytest.fixture
def mock_resend():
    """Stubs the Resend HTTP call so billing tests don't send real emails.
    Email content/delivery itself gets its own test module."""
    with patch("billing.requests.post") as mock_post:
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"id": "re_fake_id"}
        mock_post.return_value = response
        yield mock_post


@pytest.fixture
def stripe_customer():
    """A real Stripe test-mode customer with a working default payment
    method, torn down (subscriptions canceled, customer deleted) after the
    test regardless of pass/fail."""
    customer = stripe.Customer.create(email="pytest-billing@example.com")
    pm = stripe.PaymentMethod.create(type="card", card={"token": "tok_visa"})
    stripe.PaymentMethod.attach(pm.id, customer=customer.id)
    stripe.Customer.modify(customer.id, invoice_settings={"default_payment_method": pm.id})
    try:
        yield customer, pm
    finally:
        for sub in stripe.Subscription.list(customer=customer.id, limit=10).data:
            try:
                stripe.Subscription.delete(sub.id)
            except Exception:
                pass
        stripe.Customer.delete(customer.id)


def make_subscription(customer_id, pm_id, price_id):
    return stripe.Subscription.create(
        customer=customer_id, items=[{"price": price_id}], default_payment_method=pm_id,
    )


def fake_checkout_session(customer_id, subscription_id, email="pytest-billing@example.com"):
    return {
        "id": f"cs_fake_{subscription_id}",
        "customer": customer_id,
        "subscription": subscription_id,
        "customer_details": {"email": email},
    }


def patch_line_items(price_id):
    """checkout.session.completed handling looks up the session's line
    items via a real Stripe API call - there's no server-side way to drive
    a hosted Checkout Session to completion outside a browser, so this is
    the one seam every checkout test stubs. Everything else (the DB
    writes, the actual Stripe customer/subscription) is real."""
    obj = MagicMock()
    item = MagicMock()
    item.price.id = price_id
    obj.data = [item]
    return patch("stripe.checkout.Session.list_line_items", return_value=obj)


def subscription_updated_payload(subscription, price_id, status="active"):
    """Builds the plain-dict shape handle_subscription_updated expects
    (mirrors what _process_event hands it from a real webhook body)."""
    price = stripe.Price.retrieve(price_id)
    return {
        "id": subscription.id,
        "customer": subscription.customer,
        "status": status,
        "items": {"data": [{"price": {"id": price_id, "recurring": price.recurring.to_dict()}}]},
    }


def insert_api_key(owner_name="pytest-key", monthly_quota=None, rate_limit_per_minute=None,
                    revoked_at=None, expires_at=None, stripe_customer_id=None, plan_tier=None):
    """Inserts an api_keys row directly (no Stripe/checkout involved) and
    returns (raw_key, key_id) - the generic low-level primitive auth/quota
    tests build on, distinct from the Stripe-backed checkout() helper in
    test_billing.py."""
    import secrets
    from auth import hash_key

    raw_key = secrets.token_urlsafe(32)
    key_hash = hash_key(raw_key)
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO api_keys (key_hash, owner_name, monthly_quota, rate_limit_per_minute,
                                   revoked_at, expires_at, stripe_customer_id, plan_tier)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (key_hash, owner_name, monthly_quota, rate_limit_per_minute,
             revoked_at, expires_at, stripe_customer_id, plan_tier),
        )
        key_id = cur.fetchone()["id"]
    return raw_key, key_id


def make_tool_context(tool_name="test_tool", arguments=None):
    """A MiddlewareContext stand-in with just what UsageLoggingMiddleware
    reads (context.message.name/.arguments). Values are set explicitly
    (not left as auto-MagicMock attributes) because they get written to
    usage_log - a MagicMock there would fail psycopg2's type adaptation."""
    context = MagicMock()
    context.message.name = tool_name
    context.message.arguments = arguments or {}
    return context


def run_pipeline(access_token, rate_limiter, tool_name="test_tool", tool_result="ok"):
    """Composes the real server.py middleware chain - rate limiting
    (outermost) -> quota -> usage logging (innermost) -> the actual tool -
    around a fake tool call. Mirrors mcp.add_middleware() order in
    server.py exactly, so tests using this exercise the real interaction
    between middlewares rather than each one in isolation.

    rate_limiter must be passed in (not created here) so its in-memory
    window state persists/can be inspected across multiple calls within
    one test - quota and usage-logging middleware are stateless (all their
    state lives in the DB), so fresh instances per call are harmless."""
    import asyncio
    import quota as quota_mod
    import usage_logging as usage_logging_mod

    quota_mw = quota_mod.QuotaMiddleware()
    usage_mw = usage_logging_mod.UsageLoggingMiddleware()
    context = make_tool_context(tool_name)

    async def actual_tool(ctx):
        return tool_result

    async def usage_call_next(ctx):
        return await usage_mw.on_call_tool(ctx, actual_tool)

    async def quota_call_next(ctx):
        return await quota_mw.on_call_tool(ctx, usage_call_next)

    with patch("rate_limit.get_access_token", return_value=access_token), \
         patch("quota.get_access_token", return_value=access_token), \
         patch("usage_logging.get_access_token", return_value=access_token):
        return asyncio.run(rate_limiter.on_request(context, quota_call_next))
