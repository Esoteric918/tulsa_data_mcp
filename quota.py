from mcp import McpError
from mcp.types import ErrorData

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

from db import get_cursor


class QuotaExceededError(McpError):
    def __init__(self, message: str = "Monthly quota exceeded"):
        super().__init__(ErrorData(code=-32001, message=message))


class QuotaMiddleware(Middleware):
    """Rejects a tool call once successful calls this calendar month reach
    monthly_quota (NULL = unlimited). Usage is counted from usage_log's
    success=TRUE rows, so quota rejections themselves - which never reach
    UsageLoggingMiddleware - can't inflate the count.

    For Stripe-backed keys, usage is summed across every api_keys row
    sharing the same stripe_customer_id, not just this key's own rows.
    Canceling a subscription and immediately resubscribing mints a brand
    new api_keys row (see handle_checkout_completed in billing.py) - if
    usage were scoped to that new row alone, cancel+resubscribe would hand
    back a full quota mid-month. Hand-issued keys (generate_api_key.py)
    have no stripe_customer_id, so those fall back to per-key counting."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        access_token = get_access_token()
        if access_token is None:
            return await call_next(context)  # auth is mandatory; shouldn't happen

        monthly_quota = access_token.claims.get("monthly_quota")
        if monthly_quota is None:
            return await call_next(context)

        api_key_id = int(access_token.client_id)
        stripe_customer_id = access_token.claims.get("stripe_customer_id")
        with get_cursor() as cur:
            if stripe_customer_id:
                cur.execute(
                    """
                    SELECT count(*) AS used
                    FROM usage_log ul
                    JOIN api_keys ak ON ak.id = ul.api_key_id
                    WHERE ak.stripe_customer_id = %s
                      AND ul.success = TRUE
                      AND ul.called_at >= date_trunc('month', NOW())
                    """,
                    (stripe_customer_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT count(*) AS used
                    FROM usage_log
                    WHERE api_key_id = %s
                      AND success = TRUE
                      AND called_at >= date_trunc('month', NOW())
                    """,
                    (api_key_id,),
                )
            used = cur.fetchone()["used"]

        if used >= monthly_quota:
            raise QuotaExceededError(f"Monthly quota of {monthly_quota} calls exceeded ({used} used this month)")

        return await call_next(context)
