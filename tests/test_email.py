"""Email delivery tests (billing.py's _deliver_key and _notify_payment_failed).

Most of what a generic "email delivery" test plan would list is already
covered elsewhere - confirmed by reading test_billing.py/test_auth.py and
billing.py itself before writing anything here, not assumed:

- Key delivery email's raw key content is already covered by
  test_auth.py::test_raw_key_never_persisted_stripe_webhook_path, which
  parses the real mocked Resend call to extract and verify the raw key.
  It doesn't assert the to/subject/tier fields though - this file adds one
  test that does, rather than re-testing the raw-key extraction.
- Payment-failure notification TIMING (sent once per failure episode, not
  re-sent while payment_failed_notified_at is set, re-sent on a new
  episode) is already covered by test_billing.py's TestSubscriptionUpdated.
  Not repeated here.
- "Resend failure during payment-failure notification doesn't set
  payment_failed_notified_at" is already directly covered by
  test_billing.py::test_payment_failure_notified_flag_not_set_if_send_fails.
  Not repeated here.

Genuinely new coverage added here (confirmed as gaps by grepping the
existing suite before writing anything):
- Payment-failure email CONTENT correctness (subject, grace-period
  language, tier mention).
- Key-delivery email's recipient/subject/tier fields (the raw key itself
  is already covered elsewhere, as above).
- Resend failure during KEY DELIVERY doesn't crash the webhook handler or
  cause a duplicate key - no existing test makes _deliver_key's
  requests.post actually fail. Read _deliver_key closely: it wraps
  post/raise_for_status/.json() in one broad try/except that logs and
  swallows, specifically so the webhook still returns 200 to Stripe and
  never triggers the retry that would re-run handle_checkout_completed
  (whose INSERT has no dedup guard of its own).
- Malformed/unexpected Resend responses (not just network-level failures)
  for both paths - since each path handles all exceptions uniformly
  (_deliver_key swallows anything, _notify_payment_failed propagates
  anything), one malformed-response test per path is enough to confirm
  that generalization, without re-testing every failure variant already
  covered for the plain network-failure case.
"""
from unittest.mock import MagicMock, patch

import stripe

import billing
from db import get_cursor

from conftest import MANUAL_PRICE, make_subscription, fake_checkout_session, patch_line_items


def _checkout_session_event(customer_id, subscription_id, event_id="evt_email_test"):
    session = fake_checkout_session(customer_id, subscription_id)
    return {"id": event_id, "type": "checkout.session.completed", "data": {"object": session}}


class TestKeyDeliveryEmailContent:
    def test_email_has_correct_recipient_subject_and_tier(self, mock_resend, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        session = fake_checkout_session(customer.id, sub.id, email="specific-recipient@example.com")

        with patch_line_items(MANUAL_PRICE):
            billing.handle_checkout_completed(session)

        sent = mock_resend.call_args.kwargs["json"]
        assert sent["to"] == ["specific-recipient@example.com"]
        assert sent["subject"] == "Your CountyLayer API key"
        assert "manual plan" in sent["text"]


class TestPaymentFailureEmailContent:
    def test_email_has_correct_subject_tier_and_grace_period_language(self, mock_resend, stripe_customer):
        """The past_due notification's recipient comes from the Stripe
        Customer's own .email (see handle_subscription_updated:
        stripe.Customer.retrieve(...).email) - NOT from whatever email was
        on the original checkout session. Confirmed by setting the two to
        different values and asserting the Customer's email wins."""
        customer, pm = stripe_customer
        stripe.Customer.modify(customer.id, email="the-actual-stripe-customer-email@example.com")
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        session = fake_checkout_session(customer.id, sub.id, email="unrelated-checkout-session-email@example.com")
        with patch_line_items(MANUAL_PRICE):
            billing.handle_checkout_completed(session)
        mock_resend.reset_mock()  # drop the key-delivery call

        billing.handle_subscription_updated({"id": sub.id, "customer": customer.id, "status": "past_due"})

        sent = mock_resend.call_args.kwargs["json"]
        assert sent["to"] == ["the-actual-stripe-customer-email@example.com"]
        assert sent["subject"] == "Action needed: your CountyLayer payment didn't go through"
        assert "manual plan" in sent["text"]
        assert "API key is still active" in sent["text"]  # confirms the grace-period framing, not an immediate cutoff
        assert "retry the charge" in sent["text"]


class TestKeyDeliveryFailureHandling:
    def test_resend_network_failure_does_not_crash_webhook_or_duplicate_key(self, stripe_customer):
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        event = _checkout_session_event(customer.id, sub.id)

        with patch_line_items(MANUAL_PRICE), \
             patch("billing.requests.post", side_effect=RuntimeError("network down")):
            status, body = billing._process_event(event)

        # The webhook still reports success to Stripe - by design, so Stripe
        # never retries this event (a retry would re-run
        # handle_checkout_completed, whose INSERT has no dedup guard, and
        # mint a second key for the same subscription).
        assert status == 200
        assert body == {"status": "ok"}

        with get_cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM api_keys WHERE stripe_subscription_id = %s", (sub.id,))
            assert cur.fetchone()["n"] == 1

        with get_cursor() as cur:
            cur.execute("SELECT * FROM stripe_events WHERE event_id = %s", (event["id"],))
            assert cur.fetchone() is not None  # claim held, not released - confirms no retry is expected

    def test_malformed_resend_response_handled_same_as_network_failure(self, stripe_customer):
        """resp.raise_for_status() doesn't raise (e.g. Resend returned 200),
        but resp.json() blows up parsing the body - still caught by
        _deliver_key's single broad except, same as a network failure."""
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        event = _checkout_session_event(customer.id, sub.id, event_id="evt_malformed_resend")

        bad_response = MagicMock()
        bad_response.raise_for_status = MagicMock()  # doesn't raise
        bad_response.json.side_effect = ValueError("not valid JSON")

        with patch_line_items(MANUAL_PRICE), \
             patch("billing.requests.post", return_value=bad_response):
            status, body = billing._process_event(event)

        assert status == 200
        assert body == {"status": "ok"}
        with get_cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM api_keys WHERE stripe_subscription_id = %s", (sub.id,))
            assert cur.fetchone()["n"] == 1


class TestPaymentFailureMalformedResponse:
    def test_malformed_resend_response_leaves_notified_flag_unset(self, stripe_customer):
        """Same generalization on the other path: _notify_payment_failed has
        no try/except at all, so a malformed response propagates exactly
        like a network failure - already proven for the network-failure
        case by test_billing.py's test_payment_failure_notified_flag_not_set_if_send_fails."""
        customer, pm = stripe_customer
        sub = make_subscription(customer.id, pm.id, MANUAL_PRICE)
        with patch_line_items(MANUAL_PRICE), patch("billing.requests.post") as setup_post:
            ok_response = MagicMock()
            ok_response.raise_for_status = MagicMock()
            ok_response.json.return_value = {"id": "re_setup"}
            setup_post.return_value = ok_response
            billing.handle_checkout_completed(fake_checkout_session(customer.id, sub.id))
        with get_cursor() as cur:
            cur.execute("SELECT id FROM api_keys WHERE stripe_subscription_id = %s", (sub.id,))
            key_id = cur.fetchone()["id"]

        bad_response = MagicMock()
        bad_response.raise_for_status = MagicMock()
        bad_response.json.side_effect = ValueError("not valid JSON")

        with patch("billing.requests.post", return_value=bad_response):
            try:
                billing.handle_subscription_updated({"id": sub.id, "customer": customer.id, "status": "past_due"})
            except ValueError:
                pass  # expected - propagates so Stripe's webhook retry can try again

        with get_cursor() as cur:
            cur.execute("SELECT payment_failed_notified_at FROM api_keys WHERE id = %s", (key_id,))
            assert cur.fetchone()["payment_failed_notified_at"] is None
