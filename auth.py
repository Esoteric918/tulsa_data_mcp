import hashlib

from fastmcp.server.auth import AccessToken, TokenVerifier

from db import get_cursor


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


class PostgresApiKeyVerifier(TokenVerifier):
    """Validates bearer tokens against the api_keys table. Only key hashes
    are ever compared/stored - the raw key never touches the database."""

    async def verify_token(self, token: str) -> AccessToken | None:
        key_hash = hash_key(token)
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, owner_name, monthly_quota, rate_limit_per_minute, plan_tier, stripe_customer_id
                FROM api_keys
                WHERE key_hash = %s
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > NOW())
                """,
                (key_hash,),
            )
            row = cur.fetchone()

        if row is None:
            return None

        return AccessToken(
            token=token,
            client_id=str(row["id"]),
            scopes=[],
            claims={
                "owner_name": row["owner_name"],
                "monthly_quota": row["monthly_quota"],
                "rate_limit_per_minute": row["rate_limit_per_minute"],
                "plan_tier": row["plan_tier"],
                "stripe_customer_id": row["stripe_customer_id"],
            },
        )
