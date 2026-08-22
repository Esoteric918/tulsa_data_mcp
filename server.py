import logging
import os
from datetime import date, datetime
from decimal import Decimal

from dotenv import load_dotenv

from fastmcp import FastMCP

from auth import PostgresApiKeyVerifier
from billing import stripe_webhook_route
from db import get_cursor
from quota import QuotaMiddleware
from rate_limit import PerKeyRateLimitMiddleware
from usage_logging import UsageLoggingMiddleware

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60"))

mcp = FastMCP("Tulsa Data Server", auth=PostgresApiKeyVerifier())
# Order matters: first-added middleware runs outermost, so rate limiting
# (cheap, in-memory) rejects first, then quota (one DB query), and only
# then does the tool actually run with usage logging wrapped around it.
mcp.add_middleware(PerKeyRateLimitMiddleware(default_limit=RATE_LIMIT_PER_MINUTE))
mcp.add_middleware(QuotaMiddleware())
mcp.add_middleware(UsageLoggingMiddleware())

mcp.custom_route("/webhooks/stripe", methods=["POST"])(stripe_webhook_route)

SEARCH_RESULT_LIMIT = 20


def _jsonable(value):
    """psycopg2 returns Decimal for NUMERIC and date for DATE columns - neither
    is JSON-serializable as-is, so coerce them to plain str/float."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _serialize_row(row):
    return {k: _jsonable(v) for k, v in row.items()}


SUMMARY_FIELDS = """
    account_no, owner_name, property_address, property_city, property_zip,
    total_acct_value, sale_date, sale_price
"""


@mcp.tool
def search_properties(address: str) -> dict:
    """Look up property records by street address. Matches partial addresses.

    Args:
        address: Street address or partial address to search for (e.g. "123 Main" or "Main St")
    """
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT {SUMMARY_FIELDS}
            FROM properties
            WHERE property_address ILIKE %s
            ORDER BY property_address
            LIMIT %s
            """,
            (f"%{address}%", SEARCH_RESULT_LIMIT),
        )
        rows = cur.fetchall()

    if not rows:
        return {"error": f"No records found for address: {address}"}
    return {"count": len(rows), "results": [_serialize_row(r) for r in rows]}


@mcp.tool
def lookup_owner(owner_name: str) -> dict:
    """Look up all properties owned by a given name. Matches partial names.

    Args:
        owner_name: Owner name or partial name to search for (e.g. "Smith" or "Jane Doe")
    """
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT {SUMMARY_FIELDS}
            FROM properties
            WHERE owner_name ILIKE %s
            ORDER BY owner_name
            LIMIT %s
            """,
            (f"%{owner_name}%", SEARCH_RESULT_LIMIT),
        )
        rows = cur.fetchall()

    if not rows:
        return {"error": f"No records found for owner: {owner_name}"}
    return {"count": len(rows), "results": [_serialize_row(r) for r in rows]}


@mcp.tool
def get_property_details(account_no: str) -> dict:
    """Get the full record for a single property by its account number.

    Args:
        account_no: The property's account number, as returned by search_properties or lookup_owner
    """
    with get_cursor() as cur:
        cur.execute("SELECT * FROM properties WHERE account_no = %s", (account_no,))
        row = cur.fetchone()

    if row is None:
        return {"error": f"No property found with account_no: {account_no}"}
    return _serialize_row(row)


if __name__ == "__main__":
    mcp.run(transport="http", host=MCP_HOST, port=MCP_PORT)
