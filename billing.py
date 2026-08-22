import json
import logging
import os
import secrets
from datetime import datetime, timedelta

import anyio
import requests
import stripe
from starlette.requests import Request
from starlette.responses import JSONResponse

from auth import hash_key
from db import get_cursor

log = logging.getLogger(__name__)

STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
stripe.api_key = STRIPE_SECRET_KEY

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "keys@countylayer.com")


REQUIRED_PRICE_METADATA = ("tier", "monthly_quota", "rate_limit_per_minute")

_DAYS_PER_INTERVAL_UNIT = {"day": 1, "week": 7, "month": 30, "year": 365}


def _cooldown_window(recurring: dict) -> timedelta:
    """Approximates the subscription's own billing-cycle length, used to
    size the tier-change cooldown window (see handle_subscription_updated).
    Day-count approximation for month/year is fine here - this sizes an
    anti-abuse cooldown, not an actual invoice, and being off by a day or
    two either direction doesn't matter for that purpose."""
    return timedelta(days=_DAYS_PER_INTERVAL_UNIT[recurring["interval"]] * recurring["interval_count"])


def _price_metadata(price_id: str) -> dict:
    """Tier/quota/rate-limit live on the Stripe Price itself, not a
    hardcoded mapping here - single source of truth stays in Stripe.

    Raises if any of the three fields are missing rather than defaulting
    them: monthly_quota=None means unlimited elsewhere in this codebase
    (see quota.py, used deliberately for hand-issued admin keys), so a
    Price that's merely missing its metadata - a Stripe dashboard typo -
    would otherwise silently grant unlimited API access. Better to fail
    the webhook loudly (Stripe retries it for days, giving time to fix the
    Price's metadata) than provision a key with the wrong entitlements.

    Note: this SDK version's StripeObject doesn't support .get() the way a
    normal dict does (falls through to __getattr__ and raises) - only
    bracket access and `in` work reliably, so that's what we use here."""
    price = stripe.Price.retrieve(price_id)
    metadata = price.metadata
    missing = [key for key in REQUIRED_PRICE_METADATA if key not in metadata]
    if missing:
        raise ValueError(f"Price {price_id} is missing required metadata: {', '.join(missing)}")
    return {
        "tier": metadata["tier"],
        "monthly_quota": int(metadata["monthly_quota"]),
        "rate_limit_per_minute": int(metadata["rate_limit_per_minute"]),
    }


def _deliver_key(email: str | None, raw_key: str, plan_tier: str, key_id: int):
    # Failures here are logged, never raised: the api_key row is already
    # committed by the time this runs, and _process_event's retry-on-error
    # path would otherwise call handle_checkout_completed again on the next
    # Stripe webhook retry, minting a second key for the same subscription.
    if not email:
        log.warning(f"No email on checkout session - cannot deliver api_key id={key_id}, raw key only in this log line: {raw_key}")
        return

    if not RESEND_API_KEY:
        log.warning(f"RESEND_API_KEY not configured - cannot deliver api_key id={key_id} to {email!r}, raw key only in this log line: {raw_key}")
        return

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": f"CountyLayer <{RESEND_FROM_EMAIL}>",
                "to": [email],
                "subject": "Your CountyLayer API key",
                "text": (
                    f"Thanks for subscribing to CountyLayer ({plan_tier} plan).\n\n"
                    f"Your API key:\n{raw_key}\n\n"
                    "Keep this key secret - it authenticates every request to the "
                    "MCP server and this is the only time it will be shown. If you "
                    "lose it, contact support to have it reset.\n"
                ),
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info(f"Emailed api_key id={key_id} to {email!r} (resend id={resp.json().get('id')})")
    except Exception as e:
        log.error(f"Failed to email api_key id={key_id} to {email!r}: {e} - raw key only in this log line: {raw_key}")


def _notify_payment_failed(email: str | None, plan_tier: str, key_id: int):
    """Unlike _deliver_key, failures here are allowed to raise: the caller
    only commits payment_failed_notified_at once this returns without
    error, so a raise leaves that flag NULL and lets Stripe's webhook
    retry (over the following days) try the send again. The two early
    returns below are for conditions retrying can't fix - no email on
    file, or Resend not configured - so those are logged and treated as
    handled rather than retried forever."""
    if not email:
        log.warning(f"No email on file - cannot notify api_key id={key_id} of failed payment")
        return

    if not RESEND_API_KEY:
        log.warning(f"RESEND_API_KEY not configured - cannot notify api_key id={key_id} of failed payment")
        return

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": f"CountyLayer <{RESEND_FROM_EMAIL}>",
            "to": [email],
            "subject": "Action needed: your CountyLayer payment didn't go through",
            "text": (
                f"Your latest payment for your CountyLayer ({plan_tier} plan) subscription "
                "failed - most often this means a card expired or a bank declined the charge.\n\n"
                "Your API key is still active and nothing has changed yet. We'll automatically "
                "retry the charge over the next couple of weeks; if none of those attempts "
                "succeed, your subscription will be canceled and your API key will be revoked.\n\n"
                "To avoid any interruption, update your payment method as soon as you can. "
                "Reply to this email if you need a hand.\n"
            ),
        },
        timeout=10,
    )
    resp.raise_for_status()
    log.info(f"Emailed payment-failure notice for api_key id={key_id} to {email!r} (resend id={resp.json().get('id')})")


def handle_checkout_completed(session: dict):
    customer_id = session["customer"]
    subscription_id = session["subscription"]
    customer_details = session["customer_details"] if "customer_details" in session else None
    email = customer_details["email"] if customer_details and "email" in customer_details else None

    line_items = stripe.checkout.Session.list_line_items(session["id"], limit=1)
    price_id = line_items.data[0].price.id
    meta = _price_metadata(price_id)

    raw_key = secrets.token_urlsafe(32)
    key_hash = hash_key(raw_key)

    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO api_keys (
                key_hash, owner_name, monthly_quota, rate_limit_per_minute,
                stripe_customer_id, stripe_subscription_id, plan_tier
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                key_hash, email or customer_id, meta["monthly_quota"], meta["rate_limit_per_minute"],
                customer_id, subscription_id, meta["tier"],
            ),
        )
        key_id = cur.fetchone()["id"]

    log.info(f"Created api_key id={key_id} customer={customer_id} subscription={subscription_id} tier={meta['tier']}")
    _deliver_key(email, raw_key, meta["tier"], key_id)


def handle_subscription_updated(subscription: dict):
    subscription_id = subscription["id"]
    customer_id = subscription["customer"]
    status = subscription["status"]

    # Any external API calls a branch below needs happen here, before the
    # locked transaction - never hold a DB row lock open across a
    # Stripe/Resend network round trip (only 20 pooled connections, shared
    # with every customer-facing tool call).
    customer_email = None
    if status == "past_due":
        customer_email = stripe.Customer.retrieve(customer_id).email

    new_meta = None
    new_recurring = None
    if status == "active":
        item = subscription["items"]["data"][0]
        new_meta = _price_metadata(item["price"]["id"])
        new_recurring = item["price"]["recurring"]

    # SELECT ... FOR UPDATE (on both the api_keys row and the customer's
    # cooldown row) and this branch's write happen in one transaction, so a
    # second subscription.updated event for the same subscription (different
    # event_id, different worker thread) blocks here until this commits,
    # instead of both reading stale state and racing to overwrite each
    # other. Locking the cooldown row too matters specifically for
    # cancel+resubscribe: the old (now-canceled) and new subscription are
    # different api_keys rows but the same customer, so a race between them
    # has to serialize on something keyed by customer, not by subscription.
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            SELECT id, plan_tier, monthly_quota, rate_limit_per_minute,
                   payment_failed_notified_at
            FROM api_keys WHERE stripe_subscription_id = %s
            FOR UPDATE
            """,
            (subscription_id,),
        )
        row = cur.fetchone()

        if row is None:
            log.warning(f"subscription.updated for unknown subscription_id={subscription_id} (status={status})")
            return

        key_id = row["id"]

        cur.execute(
            "INSERT INTO customer_cooldowns (stripe_customer_id) VALUES (%s) ON CONFLICT (stripe_customer_id) DO NOTHING",
            (customer_id,),
        )
        cur.execute(
            "SELECT tier_change_cooldown_until FROM customer_cooldowns WHERE stripe_customer_id = %s FOR UPDATE",
            (customer_id,),
        )
        cooldown_until = cur.fetchone()["tier_change_cooldown_until"]

        # unpaid/incomplete_expired are states our account's dunning
        # settings don't normally produce (ours cancels outright instead,
        # which arrives as customer.subscription.deleted - see
        # handle_subscription_deleted), but kept here in case that ever
        # changes: revoke immediately, no grace.
        if status in ("unpaid", "incomplete_expired"):
            cur.execute("UPDATE api_keys SET revoked_at = NOW() WHERE id = %s AND revoked_at IS NULL", (key_id,))
            log.info(f"Revoked api_key id={key_id} - subscription {subscription_id} status={status}")
            return

        # First (and any subsequent) failed renewal charge: don't revoke. A
        # single decline is often an expired card or a transient bank
        # issue, not a bad actor - so access stays live through Stripe's
        # own Smart Retries window (our account cancels the subscription
        # once retries exhaust, which fires customer.subscription.deleted
        # and revokes for real via handle_subscription_deleted). We just
        # notify the customer once per failure episode so they can fix
        # their card before that happens; payment_failed_notified_at is
        # cleared below when the subscription goes active again, so a
        # later failure notifies again.
        if status == "past_due":
            if row["payment_failed_notified_at"] is None:
                # Notify before committing the flag, not after: if this
                # raises, the flag stays NULL and _process_event's
                # retry-on-error path leaves the webhook to be redelivered
                # by Stripe, which retries the notification instead of
                # silently losing it.
                _notify_payment_failed(customer_email, row["plan_tier"], key_id)
                cur.execute("UPDATE api_keys SET payment_failed_notified_at = NOW() WHERE id = %s", (key_id,))
            return

        if status == "active":
            is_tier_change = new_meta["tier"] != row["plan_tier"]
            is_increase = (
                new_meta["monthly_quota"] > (row["monthly_quota"] or 0)
                or new_meta["rate_limit_per_minute"] > (row["rate_limit_per_minute"] or 0)
            )
            now = datetime.utcnow()
            in_cooldown = cooldown_until is not None and now < cooldown_until

            if is_tier_change and is_increase and in_cooldown:
                # Cooldown: this CUSTOMER already changed tier once inside
                # their current cooldown window. Don't grant the increase -
                # keep enforcing the tier already on file - so briefly
                # upgrading for burst rate-limit capacity and downgrading
                # right after doesn't actually get you the higher limits.
                # Keyed on customer (customer_cooldowns), not on this
                # subscription/api_keys row, and measured against a
                # wall-clock deadline we computed ourselves rather than
                # Stripe's current_period_start - so canceling and
                # immediately resubscribing (a fresh subscription, fresh
                # api_keys row, fresh Stripe current_period_start) can't
                # reset this clock. Stripe's own price/billing already
                # reflects the change; we're only withholding the
                # entitlement side until the cooldown expires, at which
                # point this same branch grants normally.
                cur.execute(
                    "UPDATE api_keys SET revoked_at = NULL, payment_failed_notified_at = NULL WHERE id = %s",
                    (key_id,),
                )
                log.warning(
                    f"Tier-change cooldown: customer={customer_id} (api_key id={key_id}) tried "
                    f"{row['plan_tier']}->{new_meta['tier']} again before its cooldown (until "
                    f"{cooldown_until}) expired - keeping {row['plan_tier']} entitlements"
                )
                return

            # Un-revoke in case this is a payment recovering from past_due,
            # and resync tier/quota/rate-limit - takes effect immediately,
            # not at next billing cycle, for both upgrades and downgrades.
            # Only refresh the customer's cooldown when this is an actual
            # tier change, so an unrelated active-status resync (e.g.
            # recovering from past_due on the same price) doesn't disturb
            # the cooldown tracking.
            cur.execute(
                """
                UPDATE api_keys
                SET revoked_at = NULL, monthly_quota = %s, rate_limit_per_minute = %s, plan_tier = %s,
                    payment_failed_notified_at = NULL
                WHERE id = %s
                """,
                (new_meta["monthly_quota"], new_meta["rate_limit_per_minute"], new_meta["tier"], key_id),
            )
            if is_tier_change:
                new_cooldown_until = now + _cooldown_window(new_recurring)
                cur.execute(
                    "UPDATE customer_cooldowns SET tier_change_cooldown_until = %s WHERE stripe_customer_id = %s",
                    (new_cooldown_until, customer_id),
                )
            log.info(f"Synced api_key id={key_id} to tier={new_meta['tier']} (subscription {subscription_id} active)")


def handle_subscription_deleted(subscription: dict):
    subscription_id = subscription["id"]
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE api_keys SET revoked_at = NOW() WHERE stripe_subscription_id = %s AND revoked_at IS NULL",
            (subscription_id,),
        )
        log.info(f"Revoked api_key(s) for deleted subscription {subscription_id}, rows={cur.rowcount}")


def handle_charge_dispute_created(dispute: dict):
    # The Dispute object carries no "customer" field of its own (verified
    # against a real Stripe test-mode dispute payload) - only "charge", the
    # id of the disputed Charge. The Charge object does carry "customer",
    # so it has to be fetched separately. This call happens before any DB
    # work, same reasoning as the past_due branch in
    # handle_subscription_updated: never hold a DB row lock open across a
    # Stripe network round trip.
    dispute_id = dispute["id"]
    charge_id = dispute.get("charge")
    customer_id = None
    if charge_id:
        charge = stripe.Charge.retrieve(charge_id)
        customer_id = charge["customer"] if "customer" in charge and charge["customer"] else None

    if not customer_id:
        log.warning(f"charge.dispute.created {dispute_id} (charge={charge_id}) carried no resolvable customer id - cannot revoke automatically")
        return
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE api_keys SET revoked_at = NOW() WHERE stripe_customer_id = %s AND revoked_at IS NULL",
            (customer_id,),
        )
        log.info(f"Revoked api_key(s) for disputed charge {charge_id} (dispute {dispute_id}) customer={customer_id}, rows={cur.rowcount}")


def handle_charge_refunded(charge: dict):
    # charge["refunded"] is Stripe's own boolean, true only once cumulative
    # amount_refunded equals the full amount - so a partial/goodwill refund
    # (customer still paid for most of the period) doesn't revoke, only a
    # fully-refunded charge does. Same posture as disputes, but refunds are
    # more often a deliberate support gesture than a fraud signal, so they
    # don't get the same guilty-until-proven-innocent treatment.
    charge_id = charge["id"]
    if not charge["refunded"]:
        log.info(f"Partial refund on charge {charge_id} ({charge['amount_refunded']}/{charge['amount']}) - not revoking")
        return

    customer_id = charge["customer"] if "customer" in charge and charge["customer"] else None
    if not customer_id:
        log.warning(f"charge.refunded (full) for {charge_id} carried no customer id - cannot revoke automatically")
        return
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE api_keys SET revoked_at = NOW() WHERE stripe_customer_id = %s AND revoked_at IS NULL",
            (customer_id,),
        )
        log.info(f"Revoked api_key(s) for fully refunded charge {charge_id} customer={customer_id}, rows={cur.rowcount}")


EVENT_HANDLERS = {
    "checkout.session.completed": handle_checkout_completed,
    "customer.subscription.updated": handle_subscription_updated,
    "customer.subscription.deleted": handle_subscription_deleted,
    "charge.dispute.created": handle_charge_dispute_created,
    "charge.refunded": handle_charge_refunded,
}


def _process_event(event: dict) -> tuple[int, dict]:
    """All blocking DB/Stripe-API work happens here, run in a worker thread
    by the route handler below - this is a plain sync function, not async,
    since our custom_route handler doesn't get FastMCP's automatic thread
    offload the way tool functions do."""

    # Atomic claim: if this event_id was already inserted (by a prior
    # delivery of the same event), do nothing further. Stripe retries
    # webhooks on network failure/timeout, so duplicate delivery is routine,
    # not an edge case.
    with get_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO stripe_events (event_id, event_type) VALUES (%s, %s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
            (event["id"], event["type"]),
        )
        claimed = cur.fetchone() is not None

    if not claimed:
        log.info(f"Skipping already-processed event {event['id']} ({event['type']})")
        return 200, {"status": "already processed"}

    handler = EVENT_HANDLERS.get(event["type"])
    if handler is None:
        return 200, {"status": "ignored"}

    try:
        handler(event["data"]["object"])
    except Exception as e:
        log.error(f"Error handling event {event['id']} ({event['type']}): {e}")
        # Release the claim so a legitimate Stripe retry can actually
        # reprocess this event instead of being silently skipped as "done".
        with get_cursor(commit=True) as cur:
            cur.execute("DELETE FROM stripe_events WHERE event_id = %s", (event["id"],))
        return 500, {"error": "internal error"}

    return 200, {"status": "ok"}


async def stripe_webhook_route(request: Request) -> JSONResponse:
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError) as e:
        log.warning(f"Webhook signature verification failed: {e}")
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    # construct_event() only exists to verify the signature - we deliberately
    # discard its StripeObject-wrapped return value and re-parse the same
    # already-verified raw payload as plain dicts instead. This SDK's
    # StripeObject doesn't support .get() correctly, which is an easy silent
    # trap in handler code; plain dicts from json.loads sidestep it entirely.
    event = json.loads(payload)

    status_code, body = await anyio.to_thread.run_sync(_process_event, event)
    return JSONResponse(body, status_code=status_code)
