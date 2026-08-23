"""Quota tests, plus the rate-limit/quota interaction tests.

Most tests here run the REAL 3-middleware chain (rate limiting -> quota ->
usage logging, via conftest.run_pipeline) rather than calling
QuotaMiddleware in isolation - test_auth.py's TestQuotaFallbackRegression
already covers real QuotaExceededError rejections via claims routing; this
file extends that with boundary math, the quota-vs-rate-limit error
distinction, usage-log write behavior, and the interaction between the two
middlewares.
"""
import asyncio
from datetime import datetime

import pytest
from fastmcp.server.auth import AccessToken

import quota
from auth import PostgresApiKeyVerifier
from db import get_cursor
from rate_limit import PerKeyRateLimitMiddleware, RateLimitError

from conftest import (
    MANUAL_PRICE, AGENT_PRICE, insert_api_key, run_pipeline,
    stripe_customer, mock_resend, make_subscription, subscription_updated_payload, patch_line_items,
)


def _verify(raw_key):
    return asyncio.run(PostgresApiKeyVerifier().verify_token(raw_key))


def _usage_count(key_id):
    with get_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM usage_log WHERE api_key_id = %s AND success = TRUE", (key_id,))
        return cur.fetchone()["n"]


def _generous_rate_limiter():
    return PerKeyRateLimitMiddleware(default_limit=100_000)


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

class TestQuota:
    def test_key_under_quota_succeeds(self):
        raw_key, key_id = insert_api_key(monthly_quota=5)
        token = _verify(raw_key)

        result = run_pipeline(token, _generous_rate_limiter())

        assert result == "ok"
        assert _usage_count(key_id) == 1

    def test_boundary_nth_call_succeeds_and_increments_n_plus_1th_rejected(self):
        raw_key, key_id = insert_api_key(monthly_quota=3)
        token = _verify(raw_key)
        limiter = _generous_rate_limiter()

        for i in range(3):
            assert run_pipeline(token, limiter) == "ok", f"call {i + 1} of 3 should have succeeded"
        assert _usage_count(key_id) == 3

        with pytest.raises(quota.QuotaExceededError):
            run_pipeline(token, limiter)
        assert _usage_count(key_id) == 3  # the rejected 4th call left no trace

    def test_quota_exceeded_raises_quota_specific_error(self):
        raw_key, key_id = insert_api_key(monthly_quota=0)
        token = _verify(raw_key)

        with pytest.raises(quota.QuotaExceededError) as exc_info:
            run_pipeline(token, _generous_rate_limiter())
        assert exc_info.value.error.code == -32001

    def test_quota_error_and_rate_limit_error_are_distinguishable(self):
        assert quota.QuotaExceededError is not RateLimitError
        assert not issubclass(quota.QuotaExceededError, RateLimitError)
        assert not issubclass(RateLimitError, quota.QuotaExceededError)

        quota_err = quota.QuotaExceededError()
        rate_err = RateLimitError()
        assert quota_err.error.code != rate_err.error.code
        assert quota_err.error.code == -32001
        assert rate_err.error.code == -32000

    def test_quota_sums_across_customer_api_keys_real_rejection(self):
        """Regression test for the cancel+resubscribe fix, exercised
        through an actual over-quota pipeline rejection (test_auth.py's
        version of this already does this too - kept here as well since
        it's core to what this module is responsible for)."""
        old_raw_key, old_key_id = insert_api_key(
            monthly_quota=2, stripe_customer_id="cus_quota_regression", revoked_at=datetime.utcnow(),
        )
        new_raw_key, new_key_id = insert_api_key(
            monthly_quota=2, stripe_customer_id="cus_quota_regression",
        )
        with get_cursor(commit=True) as cur:
            for _ in range(2):
                cur.execute(
                    "INSERT INTO usage_log (api_key_id, tool_name, success) VALUES (%s, 'lookup_property', TRUE)",
                    (old_key_id,),
                )

        new_token = _verify(new_raw_key)
        with pytest.raises(quota.QuotaExceededError):
            run_pipeline(new_token, _generous_rate_limiter())

    def test_hand_issued_keys_with_no_customer_id_track_usage_independently(self):
        raw_key_a, key_id_a = insert_api_key(monthly_quota=1, stripe_customer_id=None)
        raw_key_b, key_id_b = insert_api_key(monthly_quota=1, stripe_customer_id=None)
        limiter = _generous_rate_limiter()

        token_a = _verify(raw_key_a)
        assert run_pipeline(token_a, limiter) == "ok"
        assert _usage_count(key_id_a) == 1
        assert _usage_count(key_id_b) == 0  # key A's usage didn't bleed into key B

        token_b = _verify(raw_key_b)
        assert run_pipeline(token_b, limiter) == "ok"  # key B has its own untouched quota
        assert _usage_count(key_id_b) == 1

        # both keys are now individually at their (separate) quota of 1
        with pytest.raises(quota.QuotaExceededError):
            run_pipeline(token_a, limiter)
        with pytest.raises(quota.QuotaExceededError):
            run_pipeline(token_b, limiter)

    def test_usage_counter_survives_tier_change_mid_cycle(self, mock_resend, stripe_customer):
        """This was a real bug found and fixed during the billing hardening
        work. Confirmed by grepping test_billing.py: neither of its
        upgrade/downgrade tests check usage_log at all, so this regression
        wasn't actually covered anywhere before this test. Goes through the
        real billing.handle_subscription_updated path (not a direct SQL
        tier flip) for fidelity to the real trigger."""
        import billing

        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        session = {
            "id": f"cs_fake_{sub.id}", "customer": customer.id, "subscription": sub.id,
            "customer_details": {"email": "pytest-billing@example.com"},
        }
        with patch_line_items(MANUAL_PRICE):
            billing.handle_checkout_completed(session)
        with get_cursor() as cur:
            cur.execute("SELECT id FROM api_keys WHERE stripe_subscription_id = %s", (sub.id,))
            key_id = cur.fetchone()["id"]

        with get_cursor(commit=True) as cur:
            for _ in range(3):
                cur.execute(
                    "INSERT INTO usage_log (api_key_id, tool_name, success) VALUES (%s, 'lookup_property', TRUE)",
                    (key_id,),
                )
        assert _usage_count(key_id) == 3

        billing.handle_subscription_updated(subscription_updated_payload(sub, AGENT_PRICE))

        assert _usage_count(key_id) == 3  # unchanged by the tier switch
        with get_cursor() as cur:
            cur.execute("SELECT plan_tier, monthly_quota FROM api_keys WHERE id = %s", (key_id,))
            row = cur.fetchone()
        assert row["plan_tier"] == "agent"
        assert row["monthly_quota"] == 10000  # new tier's quota, but usage carried forward, not reset


# ---------------------------------------------------------------------------
# Interaction between rate limiting and quota
# ---------------------------------------------------------------------------

class TestRateLimitQuotaInteraction:
    def test_rate_limited_call_does_not_consume_quota(self):
        raw_key, key_id = insert_api_key(monthly_quota=100)
        token = _verify(raw_key)
        tight_limiter = PerKeyRateLimitMiddleware(default_limit=1)

        assert run_pipeline(token, tight_limiter) == "ok"
        assert _usage_count(key_id) == 1

        with pytest.raises(RateLimitError):
            run_pipeline(token, tight_limiter)
        assert _usage_count(key_id) == 1  # the rate-limited attempt never reached quota/usage logging

    def test_quota_rejected_call_still_consumes_a_rate_limit_slot(self):
        """The asymmetric half of the interaction: rate limiting records a
        request's timestamp BEFORE calling downstream into quota, and
        doesn't refund it if quota then rejects. Proven black-box: with
        quota tight enough to reject the 2nd call, a 3rd call gets rejected
        by RATE LIMITING specifically (not quota) - proof the 2nd
        (quota-rejected) call still occupied a rate-limit slot."""
        raw_key, key_id = insert_api_key(monthly_quota=1)
        token = _verify(raw_key)
        limiter = PerKeyRateLimitMiddleware(default_limit=2)

        assert run_pipeline(token, limiter) == "ok"                       # call 1: rate slot 1/2, quota 1/1
        with pytest.raises(quota.QuotaExceededError):
            run_pipeline(token, limiter)                                   # call 2: rate slot 2/2, quota rejects
        with pytest.raises(RateLimitError):
            run_pipeline(token, limiter)                                   # call 3: rate limit rejects first

        assert _usage_count(key_id) == 1  # only the one genuinely successful call was ever logged
