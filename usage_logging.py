import logging

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from psycopg2.extras import Json

from db import get_cursor

log = logging.getLogger(__name__)


class UsageLoggingMiddleware(Middleware):
    """Logs every tool call (who, what, when, success/failure) to usage_log.
    A failure to write the log never breaks the actual tool response -
    this is for billing/debugging, not something a client request should
    fail over."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        access_token = get_access_token()
        api_key_id = int(access_token.client_id) if access_token else None
        tool_name = context.message.name
        arguments = context.message.arguments

        try:
            result = await call_next(context)
        except Exception as e:
            self._log(api_key_id, tool_name, arguments, success=False, error_message=str(e))
            raise

        self._log(api_key_id, tool_name, arguments, success=True, error_message=None)
        return result

    def _log(self, api_key_id, tool_name, arguments, success, error_message):
        try:
            with get_cursor(commit=True) as cur:
                cur.execute(
                    """
                    INSERT INTO usage_log (api_key_id, tool_name, arguments, success, error_message)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (api_key_id, tool_name, Json(arguments) if arguments else None, success, error_message),
                )
        except Exception as e:
            log.error(f"Failed to write usage log for tool={tool_name!r} api_key_id={api_key_id}: {e}")
