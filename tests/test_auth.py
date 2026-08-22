"""Auth / API key tests.

Scope note: "missing/malformed Authorization header" is NOT handled by our
code. Traced the actual call chain in the installed mcp/fastmcp SDK:
  - No Authorization header at all -> rejected by fastmcp's
    RequireAuthMiddleware (checks header presence), never reaches us.
  - Header present but wrong scheme or empty token ("Bearer" with nothing
    after) -> rejected by the SDK's BearerAuthBackend.authenticate, which
    returns None before ever calling our verify_token.
  - Only once the SDK has parsed a syntactically valid "Bearer <token>"
    does it call PostgresApiKeyVerifier.verify_token(token) with the
    extracted token string.
So the tests below exercise verify_token() directly with malformed/garbage
TOKEN STRINGS (the real boundary of our code), not fabricated HTTP headers -
testing header parsing would just be testing the SDK, not us.
"""
import asyncio
import re
import secrets
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import auth
import quota
from auth import PostgresApiKeyVerifier, hash_key
from db import get_cursor

from conftest import (
    MANUAL_PRICE, insert_api_key, stripe_customer, mock_resend,
    make_subscription, fake_checkout_session, patch_line_items,
)


def _verify(token):
    return asyncio.run(PostgresApiKeyVerifier().verify_token(token))


# ---------------------------------------------------------------------------
# Key hashing & lookup
# ---------------------------------------------------------------------------

class TestKeyHashing:
    def test_same_raw_key_always_hashes_the_same(self):
        raw_key = secrets.token_urlsafe(32)
        assert hash_key(raw_key) == hash_key(raw_key)

    def test_different_raw_keys_never_collide(self):
        raw_keys = [secrets.token_urlsafe(32) for _ in range(2000)]
        hashes = {hash_key(k) for k in raw_keys}
        assert len(hashes) == len(raw_keys)

    def test_lookup_is_exact_match_only_no_partial_match(self):
        raw_key, key_id = insert_api_key()

        assert _verify(raw_key) is not None
        assert _verify(raw_key[:-1]) is None       # truncated
        assert _verify(raw_key + "x") is None      # extended
        assert _verify(raw_key[1:]) is None        # prefix stripped

    def test_key_hash_column_is_indexed(self):
        with get_cursor() as cur:
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'api_keys' AND indexdef ILIKE %s",
                ("%key_hash%",),
            )
            rows = cur.fetchall()
        assert len(rows) >= 1, "expected key_hash to be covered by an index"
        assert "btree" in rows[0]["indexdef"].lower()


# ---------------------------------------------------------------------------
# Request validation (verify_token's own boundary - see module docstring)
# ---------------------------------------------------------------------------

class TestTokenValidation:
    @pytest.mark.parametrize("garbage_token", [
        "",
        "not-a-real-key",
        "a" * 10000,
        "'; DROP TABLE api_keys; --",
        "\x00\x01\x02binary-ish",
        "🔑" * 50,
    ], ids=["empty", "plausible-but-fake", "oversized", "sql-injection-shaped", "binary-ish", "unicode-heavy"])
    def test_malformed_or_garbage_token_rejected_without_crashing(self, garbage_token):
        assert _verify(garbage_token) is None

    def test_wrong_or_nonexistent_key_rejected(self):
        insert_api_key()  # a real key exists in the table...
        assert _verify(secrets.token_urlsafe(32)) is None  # ...but this one was never issued

    def test_valid_active_key_succeeds_with_correct_claims(self):
        raw_key, key_id = insert_api_key(
            owner_name="Jane Doe", monthly_quota=500, rate_limit_per_minute=30,
            stripe_customer_id="cus_fake123", plan_tier="agent",
        )

        access_token = _verify(raw_key)

        assert access_token is not None
        assert access_token.client_id == str(key_id)
        assert access_token.claims["owner_name"] == "Jane Doe"
        assert access_token.claims["monthly_quota"] == 500
        assert access_token.claims["rate_limit_per_minute"] == 30
        assert access_token.claims["stripe_customer_id"] == "cus_fake123"
        assert access_token.claims["plan_tier"] == "agent"

    def test_revoked_key_rejected(self):
        raw_key, key_id = insert_api_key(revoked_at=datetime.utcnow())
        assert _verify(raw_key) is None

    def test_expired_key_rejected(self):
        raw_key, key_id = insert_api_key(expires_at=datetime.utcnow() - timedelta(days=1))
        assert _verify(raw_key) is None

    def test_key_with_no_expiry_set_never_rejected_for_expiry(self):
        raw_key, key_id = insert_api_key(expires_at=None)
        assert _verify(raw_key) is not None

    def test_key_expiring_in_the_future_still_works(self):
        raw_key, key_id = insert_api_key(expires_at=datetime.utcnow() + timedelta(days=1))
        assert _verify(raw_key) is not None


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------

class TestKeyGeneration:
    def test_generated_keys_are_unique_across_many_generations(self):
        keys = {secrets.token_urlsafe(32) for _ in range(5000)}
        assert len(keys) == 5000

    def test_raw_key_never_persisted_manual_admin_path(self, monkeypatch, capsys):
        import sys
        import generate_api_key

        monkeypatch.setattr(sys, "argv", ["generate_api_key.py", "pytest-admin", "--monthly-quota", "500"])
        generate_api_key.main()
        output = capsys.readouterr().out

        match = re.search(r"^\s*([A-Za-z0-9_-]{40,})\s*$", output, re.MULTILINE)
        assert match, f"couldn't find the printed raw key in output:\n{output}"
        raw_key = match.group(1)

        with get_cursor() as cur:
            cur.execute("SELECT key_hash FROM api_keys WHERE owner_name = 'pytest-admin'")
            row = cur.fetchone()
        assert row is not None
        assert row["key_hash"] == hash_key(raw_key)
        assert row["key_hash"] != raw_key

        # The raw key literal must not appear stored in any column a lookup
        # could reveal it through.
        with get_cursor() as cur:
            cur.execute("SELECT * FROM api_keys WHERE key_hash = %s OR owner_name = %s", (raw_key, raw_key))
            assert cur.fetchone() is None

    def test_raw_key_never_persisted_stripe_webhook_path(self, mock_resend, stripe_customer):
        import billing

        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        session = fake_checkout_session(customer.id, sub.id)

        with patch_line_items(MANUAL_PRICE):
            billing.handle_checkout_completed(session)

        # The raw key only ever exists in the (mocked) Resend email body -
        # extract it from there, same as a real customer would receive it.
        sent_text = mock_resend.call_args.kwargs["json"]["text"]
        match = re.search(r"Your API key:\n([A-Za-z0-9_-]{40,})\n", sent_text)
        assert match, f"couldn't find the raw key in the sent email:\n{sent_text}"
        raw_key = match.group(1)

        with get_cursor() as cur:
            cur.execute("SELECT key_hash FROM api_keys WHERE stripe_subscription_id = %s", (sub.id,))
            row = cur.fetchone()
        assert row["key_hash"] == hash_key(raw_key)

        with get_cursor() as cur:
            cur.execute("SELECT * FROM api_keys WHERE key_hash = %s OR owner_name = %s", (raw_key, raw_key))
            assert cur.fetchone() is None


# ---------------------------------------------------------------------------
# Regression coverage: auth claims -> quota's per-customer/per-key branch
# ---------------------------------------------------------------------------

async def _quota_check(access_token):
    async def call_next(ctx):
        return "ok"
    with patch("quota.get_access_token", return_value=access_token):
        return await quota.QuotaMiddleware().on_call_tool(MagicMock(), call_next)


class TestQuotaFallbackRegression:
    def test_hand_issued_key_with_no_stripe_customer_uses_per_key_quota(self):
        """generate_api_key.py never sets stripe_customer_id. Confirm
        verify_token's real claims (stripe_customer_id=None) correctly
        route QuotaMiddleware to the per-key fallback branch, and that two
        such keys don't bleed into each other's usage."""
        raw_key_a, key_id_a = insert_api_key(monthly_quota=2, stripe_customer_id=None)
        raw_key_b, key_id_b = insert_api_key(monthly_quota=2, stripe_customer_id=None)

        with get_cursor(commit=True) as cur:
            for _ in range(2):
                cur.execute(
                    "INSERT INTO usage_log (api_key_id, tool_name, success) VALUES (%s, 'lookup_property', TRUE)",
                    (key_id_a,),
                )

        token_a = _verify(raw_key_a)
        assert token_a.claims["stripe_customer_id"] is None
        with pytest.raises(quota.QuotaExceededError):
            asyncio.run(_quota_check(token_a))

        token_b = _verify(raw_key_b)
        assert asyncio.run(_quota_check(token_b)) == "ok"  # unaffected by key A's usage

    def test_stripe_backed_key_quota_still_summed_per_customer_via_real_claims(self):
        """Complements test_billing.py's cancel+resubscribe test, but
        exercises the actual auth.py -> quota.py chain (real verify_token
        claims driving the decision) rather than calling billing.py's
        handlers directly."""
        old_raw_key, old_key_id = insert_api_key(
            monthly_quota=2, stripe_customer_id="cus_regression_test", revoked_at=datetime.utcnow(),
        )
        new_raw_key, new_key_id = insert_api_key(
            monthly_quota=2, stripe_customer_id="cus_regression_test",
        )

        with get_cursor(commit=True) as cur:
            for _ in range(2):
                cur.execute(
                    "INSERT INTO usage_log (api_key_id, tool_name, success) VALUES (%s, 'lookup_property', TRUE)",
                    (old_key_id,),
                )

        new_token = _verify(new_raw_key)
        assert new_token.claims["stripe_customer_id"] == "cus_regression_test"
        with pytest.raises(quota.QuotaExceededError):
            asyncio.run(_quota_check(new_token))
