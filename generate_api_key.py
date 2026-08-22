import argparse
import secrets
from datetime import datetime, timedelta

from auth import hash_key
from db import get_cursor


def main():
    parser = argparse.ArgumentParser(description="Issue a new API key for the Tulsa Data MCP server.")
    parser.add_argument("owner_name", help="Customer/account name this key belongs to")
    parser.add_argument(
        "--expires-in-days", type=int, default=365,
        help="Days until this key expires (default: 365, matching annual subscriptions). Use 0 for a key that never expires.",
    )
    parser.add_argument(
        "--monthly-quota", type=int, default=None,
        help="Max successful tool calls per calendar month. Omit for unlimited.",
    )
    args = parser.parse_args()

    raw_key = secrets.token_urlsafe(32)
    key_hash = hash_key(raw_key)
    expires_at = None if args.expires_in_days == 0 else datetime.now() + timedelta(days=args.expires_in_days)

    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO api_keys (key_hash, owner_name, expires_at, monthly_quota)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (key_hash, args.owner_name, expires_at, args.monthly_quota),
        )
        key_id = cur.fetchone()["id"]

    print(f"Created API key id={key_id} for '{args.owner_name}'")
    print(f"  Expires: {expires_at.date() if expires_at else 'never'}")
    print(f"  Monthly quota: {args.monthly_quota if args.monthly_quota is not None else 'unlimited'}")
    print()
    print(f"  {raw_key}")
    print()
    print("This is shown once. Store it now - only its hash is kept, it cannot be recovered.")


if __name__ == "__main__":
    main()
