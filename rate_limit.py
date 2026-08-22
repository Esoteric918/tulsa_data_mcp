import time
from collections import defaultdict, deque

import anyio
from mcp import McpError
from mcp.types import ErrorData

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

WINDOW_SECONDS = 60


class RateLimitError(McpError):
    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(ErrorData(code=-32000, message=message))


class _ClientWindow:
    def __init__(self):
        self.requests: deque[float] = deque()
        self.lock = anyio.Lock()


class PerKeyRateLimitMiddleware(Middleware):
    """Sliding-window rate limiting where the limit is read fresh, per
    request, from the authenticated key's claims (rate_limit_per_minute,
    set by PostgresApiKeyVerifier) - so each Stripe plan tier carries its
    own req/min ceiling, and a tier change takes effect on the very next
    request rather than requiring a restart or reconnect.

    default_limit is the fallback for keys with no tier-specific limit set
    (e.g. manually-issued admin/test keys).
    """

    def __init__(self, default_limit: int = 60):
        self.default_limit = default_limit
        self._windows: dict[str, _ClientWindow] = defaultdict(_ClientWindow)

    async def on_request(self, context: MiddlewareContext, call_next):
        access_token = get_access_token()
        if access_token is None:
            return await call_next(context)

        client_id = access_token.client_id
        limit = access_token.claims.get("rate_limit_per_minute") or self.default_limit
        window = self._windows[client_id]

        async with window.lock:
            now = time.time()
            cutoff = now - WINDOW_SECONDS
            while window.requests and window.requests[0] < cutoff:
                window.requests.popleft()

            if len(window.requests) >= limit:
                raise RateLimitError(f"Rate limit exceeded: {limit} requests per minute for client {client_id}")

            window.requests.append(now)

        return await call_next(context)
