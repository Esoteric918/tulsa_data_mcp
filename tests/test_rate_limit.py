"""Rate limiting tests.

rate_limit.py's state is entirely in-memory (a deque of request timestamps
per client_id, held on the PerKeyRateLimitMiddleware instance) - no DB
involved at all. Every test below constructs its own fresh middleware
instance so window state never leaks between tests.

It fires on FastMCP's on_request hook, which - confirmed by reading
fastmcp's dispatcher - runs for EVERY MCP request type (tools/list,
resources/read, initialize, ...), not just tool calls. Quota and usage
logging only fire on on_call_tool (tools/call specifically). That scope
difference is covered by the interaction tests in test_quota.py.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.server.auth import AccessToken

from rate_limit import PerKeyRateLimitMiddleware, RateLimitError

MANUAL_LIMIT = 20   # req/min, matches the real Manual tier price metadata
AGENT_LIMIT = 200   # req/min, matches the real Agent tier price metadata


def _token(client_id="1", rate_limit_per_minute=None):
    return AccessToken(
        token="fake", client_id=client_id, scopes=[],
        claims={"rate_limit_per_minute": rate_limit_per_minute},
    )


class _FakeClock:
    """Deterministic, controllable replacement for time.time() - real
    sleep()-based window tests would be slow and flaky."""
    def __init__(self, start=1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _hit(middleware, access_token, clock=None):
    """Runs one request through the middleware. With no clock given, real
    time.time() is used (fine for tests that only need 1-2 quick calls);
    tests that need to control elapsed time pass a _FakeClock."""
    async def call_next(ctx):
        return "ok"

    with patch("rate_limit.get_access_token", return_value=access_token):
        if clock is not None:
            with patch("rate_limit.time.time", clock):
                return asyncio.run(middleware.on_request(MagicMock(), call_next))
        return asyncio.run(middleware.on_request(MagicMock(), call_next))


class TestRateLimit:
    def test_key_under_limit_succeeds(self):
        mw = PerKeyRateLimitMiddleware(default_limit=60)
        token = _token(rate_limit_per_minute=5)
        for _ in range(3):
            assert _hit(mw, token) == "ok"

    @pytest.mark.parametrize("limit", [MANUAL_LIMIT, AGENT_LIMIT], ids=["manual-20-per-min", "agent-200-per-min"])
    def test_key_at_and_over_tier_limit_rejected(self, limit):
        mw = PerKeyRateLimitMiddleware(default_limit=60)
        token = _token(rate_limit_per_minute=limit)
        clock = _FakeClock()

        for _ in range(limit):
            assert _hit(mw, token, clock) == "ok"

        with pytest.raises(RateLimitError):
            _hit(mw, token, clock)

    def test_boundary_nth_request_succeeds_n_plus_1th_rejected(self):
        mw = PerKeyRateLimitMiddleware(default_limit=60)
        token = _token(rate_limit_per_minute=5)
        clock = _FakeClock()

        for i in range(5):
            assert _hit(mw, token, clock) == "ok", f"request {i + 1} of 5 should have succeeded"

        with pytest.raises(RateLimitError):
            _hit(mw, token, clock)

    def test_window_resets_after_60_seconds(self):
        mw = PerKeyRateLimitMiddleware(default_limit=60)
        token = _token(rate_limit_per_minute=2)
        clock = _FakeClock()

        assert _hit(mw, token, clock) == "ok"
        assert _hit(mw, token, clock) == "ok"
        with pytest.raises(RateLimitError):
            _hit(mw, token, clock)

        clock.advance(61)
        assert _hit(mw, token, clock) == "ok"  # window has rolled forward, budget is back

    def test_partial_window_reset_only_expired_requests_drop(self):
        """Sliding window, not a fixed bucket: only entries older than 60s
        fall out, not the whole window at once."""
        mw = PerKeyRateLimitMiddleware(default_limit=60)
        token = _token(rate_limit_per_minute=2)
        clock = _FakeClock()

        assert _hit(mw, token, clock) == "ok"   # t=0
        clock.advance(30)
        assert _hit(mw, token, clock) == "ok"   # t=30, window still has both
        with pytest.raises(RateLimitError):
            _hit(mw, token, clock)              # t=30, at limit (2/2)

        clock.advance(31)                        # t=61: the t=0 request has aged out, t=30 one hasn't
        assert _hit(mw, token, clock) == "ok"    # one slot freed up
        with pytest.raises(RateLimitError):
            _hit(mw, token, clock)               # back to 2/2 (t=30 and t=61 requests)

    def test_key_with_no_tier_limit_falls_back_to_default(self):
        mw = PerKeyRateLimitMiddleware(default_limit=2)
        token = _token(rate_limit_per_minute=None)
        clock = _FakeClock()

        assert _hit(mw, token, clock) == "ok"
        assert _hit(mw, token, clock) == "ok"
        with pytest.raises(RateLimitError):
            _hit(mw, token, clock)

    def test_different_clients_have_independent_windows(self):
        mw = PerKeyRateLimitMiddleware(default_limit=60)
        token_a = _token(client_id="1", rate_limit_per_minute=1)
        token_b = _token(client_id="2", rate_limit_per_minute=1)
        clock = _FakeClock()

        assert _hit(mw, token_a, clock) == "ok"
        with pytest.raises(RateLimitError):
            _hit(mw, token_a, clock)

        assert _hit(mw, token_b, clock) == "ok"  # unaffected by client A's exhausted window

    def test_no_access_token_passes_through(self):
        mw = PerKeyRateLimitMiddleware(default_limit=60)
        async def call_next(ctx):
            return "ok"
        with patch("rate_limit.get_access_token", return_value=None):
            assert asyncio.run(mw.on_request(MagicMock(), call_next)) == "ok"
