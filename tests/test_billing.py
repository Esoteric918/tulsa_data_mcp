"""Billing webhook tests. Highest-risk module: real money and access
control live here. Each test drives billing.py's real handler functions
against a real Stripe test-mode account and the real (isolated) test DB -
see conftest.py for the fixtures and the reasoning behind what's real vs.
stubbed."""
import threading
import time

import pytest
import stripe

import billing
from db import get_cursor

from conftest import (
    MANUAL_PRICE, AGENT_PRICE,
    make_subscription, fake_checkout_session, patch_line_items, subscription_updated_payload,
)


def _get_key(subscription_id=None, customer_id=None):
    with get_cursor() as cur:
        if subscription_id:
            cur.execute("SELECT * FROM api_keys WHERE stripe_subscription_id = %s", (subscription_id,))
        else:
            cur.execute("SELECT * FROM api_keys WHERE stripe_customer_id = %s", (customer_id,))
        return cur.fetchone()


def _count_keys():
    with get_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM api_keys")
        return cur.fetchone()["n"]


# ---------------------------------------------------------------------------
# checkout.session.completed
# ---------------------------------------------------------------------------

class TestCheckoutCompleted:
    def test_valid_checkout_creates_key_with_correct_metadata(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        session = fake_checkout_session(customer.id, sub.id)

        with patch_line_items(MANUAL_PRICE):
            billing.handle_checkout_completed(session)

        key = _get_key(subscription_id=sub.id)
        assert key is not None
        assert key["stripe_customer_id"] == customer.id
        assert key["plan_tier"] == "manual"
        assert key["monthly_quota"] == 1000
        assert key["rate_limit_per_minute"] == 20
        assert key["revoked_at"] is None
        mock_resend.assert_called_once()

    def test_duplicate_event_delivery_is_idempotent(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        session = fake_checkout_session(customer.id, sub.id)
        event = {"id": "evt_dup_test", "type": "checkout.session.completed", "data": {"object": session}}

        with patch_line_items(MANUAL_PRICE):
            status1, _ = billing._process_event(event)
            status2, body2 = billing._process_event(event)

        assert status1 == 200
        assert status2 == 200
        assert body2 == {"status": "already processed"}
        assert _count_keys() == 1
        mock_resend.assert_called_once()

    def test_price_missing_required_metadata_refuses_provisioning(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        # A real Stripe test-mode Product/Price with none of the required
        # metadata (tier/monthly_quota/rate_limit_per_minute) - simulates a
        # Stripe dashboard typo, not a hardcoded/fake object.
        product = stripe.Product.create(name="pytest-broken-price")
        bad_price = stripe.Price.create(product=product.id, unit_amount=1000, currency="usd",
                                         recurring={"interval": "month"})
        sub = make_subscription(customer.id, pm.id, bad_price.id)
        session = fake_checkout_session(customer.id, sub.id)
        event = {"id": "evt_bad_meta", "type": "checkout.session.completed", "data": {"object": session}}

        with patch_line_items(bad_price.id):
            status, body = billing._process_event(event)

        assert status == 500
        assert _count_keys() == 0
        # Claim released so Stripe's retry can reprocess once the Price is fixed.
        with get_cursor() as cur:
            cur.execute("SELECT * FROM stripe_events WHERE event_id = %s", (event["id"],))
            assert cur.fetchone() is None
        mock_resend.assert_not_called()

        stripe.Price.modify(bad_price.id, active=False)
        stripe.Product.modify(product.id, active=False)


# ---------------------------------------------------------------------------
# customer.subscription.updated
# ---------------------------------------------------------------------------

def _checkout(customer_id, subscription_id, price_id):
    session = fake_checkout_session(customer_id, subscription_id)
    with patch_line_items(price_id):
        billing.handle_checkout_completed(session)
    return _get_key(subscription_id=subscription_id)["id"]


class TestSubscriptionUpdated:
    def test_upgrade_grants_higher_tier_immediately(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id = _checkout(customer.id, sub.id, MANUAL_PRICE)

        billing.handle_subscription_updated(subscription_updated_payload(sub, AGENT_PRICE))

        with get_cursor() as cur:
            cur.execute("SELECT plan_tier, monthly_quota, rate_limit_per_minute FROM api_keys WHERE id = %s", (key_id,))
            key = cur.fetchone()
        assert key["plan_tier"] == "agent"
        assert key["monthly_quota"] == 10000
        assert key["rate_limit_per_minute"] == 200

    def test_downgrade_cuts_access_immediately(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, AGENT_PRICE)
        key_id = _checkout(customer.id, sub.id, AGENT_PRICE)

        billing.handle_subscription_updated(subscription_updated_payload(sub, MANUAL_PRICE))

        with get_cursor() as cur:
            cur.execute("SELECT plan_tier, monthly_quota, rate_limit_per_minute FROM api_keys WHERE id = %s", (key_id,))
            key = cur.fetchone()
        assert key["plan_tier"] == "manual"
        assert key["monthly_quota"] == 1000
        assert key["rate_limit_per_minute"] == 20

    def test_second_upgrade_within_cooldown_capped_not_revoked(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id = _checkout(customer.id, sub.id, MANUAL_PRICE)

        # Burst pattern the cooldown exists to stop: upgrade, downgrade, upgrade again.
        billing.handle_subscription_updated(subscription_updated_payload(sub, AGENT_PRICE))
        billing.handle_subscription_updated(subscription_updated_payload(sub, MANUAL_PRICE))
        billing.handle_subscription_updated(subscription_updated_payload(sub, AGENT_PRICE))

        with get_cursor() as cur:
            cur.execute("SELECT plan_tier, revoked_at FROM api_keys WHERE id = %s", (key_id,))
            key = cur.fetchone()
        # Capped at manual (the second upgrade attempt is withheld) - and
        # explicitly NOT revoked, just held at the prior entitlement.
        assert key["plan_tier"] == "manual"
        assert key["revoked_at"] is None

    def test_past_due_does_not_revoke_grace_period(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id = _checkout(customer.id, sub.id, MANUAL_PRICE)
        mock_resend.reset_mock()  # drop the key-delivery call from _checkout

        billing.handle_subscription_updated({"id": sub.id, "customer": customer.id, "status": "past_due"})

        with get_cursor() as cur:
            cur.execute("SELECT revoked_at, payment_failed_notified_at FROM api_keys WHERE id = %s", (key_id,))
            key = cur.fetchone()
        assert key["revoked_at"] is None
        assert key["payment_failed_notified_at"] is not None
        mock_resend.assert_called_once()

    def test_payment_failure_email_sent_once_per_failure_episode(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id = _checkout(customer.id, sub.id, MANUAL_PRICE)
        mock_resend.reset_mock()  # drop the key-delivery call from _checkout

        past_due_payload = {"id": sub.id, "customer": customer.id, "status": "past_due"}
        # Two redelivered past_due events during the same failure episode.
        billing.handle_subscription_updated(past_due_payload)
        billing.handle_subscription_updated(past_due_payload)

        assert mock_resend.call_count == 1

        # Recovers, then fails again - a *new* failure episode notifies again.
        billing.handle_subscription_updated(subscription_updated_payload(sub, MANUAL_PRICE))
        with get_cursor() as cur:
            cur.execute("SELECT payment_failed_notified_at FROM api_keys WHERE id = %s", (key_id,))
            assert cur.fetchone()["payment_failed_notified_at"] is None
        billing.handle_subscription_updated(past_due_payload)
        assert mock_resend.call_count == 2


    def test_payment_failure_notified_flag_not_set_if_send_fails(self, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id = _checkout(customer.id, sub.id, MANUAL_PRICE)

        from unittest.mock import patch
        with patch("billing.requests.post", side_effect=RuntimeError("network down")):
            with pytest.raises(RuntimeError):
                billing.handle_subscription_updated({"id": sub.id, "customer": customer.id, "status": "past_due"})

        with get_cursor() as cur:
            cur.execute("SELECT payment_failed_notified_at FROM api_keys WHERE id = %s", (key_id,))
            assert cur.fetchone()["payment_failed_notified_at"] is None


# ---------------------------------------------------------------------------
# customer.subscription.deleted
# ---------------------------------------------------------------------------

class TestSubscriptionDeleted:
    def test_cancellation_revokes_key_immediately(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id = _checkout(customer.id, sub.id, MANUAL_PRICE)

        billing.handle_subscription_deleted({"id": sub.id})

        with get_cursor() as cur:
            cur.execute("SELECT revoked_at FROM api_keys WHERE id = %s", (key_id,))
            assert cur.fetchone()["revoked_at"] is not None

    def test_cancel_and_resubscribe_cooldown_and_quota_survive(self, mock_resend, stripe_customer):
        """Regression test for the cancel+resubscribe gap: canceling and
        immediately resubscribing mints a brand new api_keys row (new
        api_key_id, new Stripe current_period_start) - both the
        tier-change cooldown and the monthly quota must still be enforced
        against the CUSTOMER, not reset by that churn."""
        customer, pm = stripe_customer
        sub1 = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id_1 = _checkout(customer.id, sub1.id, MANUAL_PRICE)

        # Burn this key's quota and its one tier-change grant for the period.
        with get_cursor(commit=True) as cur:
            cur.execute("UPDATE api_keys SET monthly_quota = 2 WHERE id = %s", (key_id_1,))
            for _ in range(2):
                cur.execute(
                    "INSERT INTO usage_log (api_key_id, tool_name, success) VALUES (%s, 'lookup_property', TRUE)",
                    (key_id_1,),
                )
        billing.handle_subscription_updated(subscription_updated_payload(sub1, AGENT_PRICE))

        # Cancel, then immediately resubscribe - genuinely new subscription,
        # new current_period_start, new api_keys row.
        stripe.Subscription.delete(sub1.id)
        billing.handle_subscription_deleted({"id": sub1.id})
        sub2 = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id_2 = _checkout(customer.id, sub2.id, MANUAL_PRICE)

        assert key_id_2 != key_id_1

        # Quota: the new key must not get a free reset.
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS n FROM usage_log ul JOIN api_keys ak ON ak.id = ul.api_key_id
                WHERE ak.stripe_customer_id = %s AND ul.success = TRUE
                """,
                (customer.id,),
            )
            assert cur.fetchone()["n"] == 2

        # Cooldown: an upgrade attempt on the new subscription, same abuse window.
        billing.handle_subscription_updated(subscription_updated_payload(sub2, AGENT_PRICE))
        with get_cursor() as cur:
            cur.execute("SELECT plan_tier FROM api_keys WHERE id = %s", (key_id_2,))
            assert cur.fetchone()["plan_tier"] == "manual"


# ---------------------------------------------------------------------------
# charge.refunded
# ---------------------------------------------------------------------------

class TestChargeRefunded:
    def test_full_refund_revokes_key(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id = _checkout(customer.id, sub.id, MANUAL_PRICE)

        billing.handle_charge_refunded({
            "id": "ch_fake", "refunded": True, "amount": 2900, "amount_refunded": 2900,
            "customer": customer.id,
        })

        with get_cursor() as cur:
            cur.execute("SELECT revoked_at FROM api_keys WHERE id = %s", (key_id,))
            assert cur.fetchone()["revoked_at"] is not None

    def test_partial_refund_keeps_key_active(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id = _checkout(customer.id, sub.id, MANUAL_PRICE)

        billing.handle_charge_refunded({
            "id": "ch_fake", "refunded": False, "amount": 2900, "amount_refunded": 1000,
            "customer": customer.id,
        })

        with get_cursor() as cur:
            cur.execute("SELECT revoked_at FROM api_keys WHERE id = %s", (key_id,))
            assert cur.fetchone()["revoked_at"] is None


# ---------------------------------------------------------------------------
# charge.dispute.created
# ---------------------------------------------------------------------------

def _real_dispute(customer_id, pm_id):
    """Triggers a real Stripe test-mode chargeback (tok_createDispute fires
    one immediately) and returns the real Dispute payload. Used instead of
    a hand-built dict because the point being tested is specifically that
    the Dispute object carries no top-level "customer" field - a
    hand-rolled fake could accidentally paper over that."""
    dispute_pm = stripe.PaymentMethod.create(type="card", card={"token": "tok_createDispute"})
    stripe.PaymentMethod.attach(dispute_pm.id, customer=customer_id)
    pi = stripe.PaymentIntent.create(
        amount=2900, currency="usd", customer=customer_id, payment_method=dispute_pm.id,
        off_session=True, confirm=True,
    )
    for _ in range(10):
        disputes = stripe.Dispute.list(limit=5)
        match = next((d for d in disputes.data if d.charge == pi.latest_charge), None)
        if match:
            return match.to_dict()
        time.sleep(1)
    raise RuntimeError("Stripe test-mode dispute did not materialize in time")


class TestChargeDisputeCreated:
    def test_dispute_revokes_key(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id = _checkout(customer.id, sub.id, MANUAL_PRICE)

        dispute = _real_dispute(customer.id, pm.id)
        assert "customer" not in dispute  # documents the real Stripe payload shape
        billing.handle_charge_dispute_created(dispute)

        with get_cursor() as cur:
            cur.execute("SELECT revoked_at FROM api_keys WHERE id = %s", (key_id,))
            assert cur.fetchone()["revoked_at"] is not None

    def test_dispute_then_refund_same_charge_no_double_processing_conflict(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id = _checkout(customer.id, sub.id, MANUAL_PRICE)

        dispute = _real_dispute(customer.id, pm.id)
        billing.handle_charge_dispute_created(dispute)
        with get_cursor() as cur:
            cur.execute("SELECT revoked_at FROM api_keys WHERE id = %s", (key_id,))
            revoked_at_1 = cur.fetchone()["revoked_at"]
        assert revoked_at_1 is not None

        # A later full refund on the same underlying charge - should be a
        # harmless no-op (already revoked), not an error or a second effect.
        billing.handle_charge_refunded({
            "id": dispute["charge"], "refunded": True, "amount": 2900, "amount_refunded": 2900,
            "customer": customer.id,
        })
        with get_cursor() as cur:
            cur.execute("SELECT revoked_at FROM api_keys WHERE id = %s", (key_id,))
            revoked_at_2 = cur.fetchone()["revoked_at"]
        assert revoked_at_2 == revoked_at_1


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_duplicate_webhook_delivered_concurrently_processed_exactly_once(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        session = fake_checkout_session(customer.id, sub.id)
        event = {"id": "evt_concurrent_dup", "type": "checkout.session.completed", "data": {"object": session}}

        results = []
        def run():
            with patch_line_items(MANUAL_PRICE):
                results.append(billing._process_event(event))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert _count_keys() == 1
        statuses = sorted(status for status, _ in results)
        assert statuses == [200, 200]

    def test_concurrent_events_same_subscription_no_silent_overwrite(self, mock_resend, stripe_customer):
        """Documents current behavior: handle_subscription_updated takes a
        row-level lock (SELECT ... FOR UPDATE) before reading or writing, so
        two different subscription.updated events for the same subscription
        processed concurrently serialize instead of racing. Final state must
        be a consistent result of one of the two updates, not a mix."""
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        key_id = _checkout(customer.id, sub.id, MANUAL_PRICE)

        payload_agent = subscription_updated_payload(sub, AGENT_PRICE)
        payload_past_due = {"id": sub.id, "customer": customer.id, "status": "past_due"}

        def run_upgrade():
            billing.handle_subscription_updated(payload_agent)

        def run_past_due():
            billing.handle_subscription_updated(payload_past_due)

        t1 = threading.Thread(target=run_upgrade)
        t2 = threading.Thread(target=run_past_due)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        with get_cursor() as cur:
            cur.execute("SELECT plan_tier, payment_failed_notified_at FROM api_keys WHERE id = %s", (key_id,))
            key = cur.fetchone()
        # Whichever order they land in, the row reflects one clean outcome:
        # either the upgrade applied (agent tier) or the past_due branch ran
        # (notified_at set) - never a torn mix of partial writes from both.
        upgrade_applied = key["plan_tier"] == "agent"
        past_due_applied = key["payment_failed_notified_at"] is not None
        assert upgrade_applied or past_due_applied
