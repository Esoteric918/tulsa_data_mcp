"""MCP tool tests (search_properties, lookup_owner, get_property_details).

Two things confirmed empirically before writing any of this, not assumed:

1. @mcp.tool registers the function as a side effect and returns the
   original plain function unchanged - server.search_properties etc. are
   directly callable Python functions, not FastMCP Tool wrapper objects.
   So tool-behavior tests below call them directly, same as every other
   module in this suite calls the real business-logic function rather than
   routing through outer framework/HTTP plumbing.

2. tulsa_data_test's properties table is empty - conftest.py only applies
   schema.sql, no data. These tools query the real `properties` table
   (not a mock), so tests seed small synthetic rows (see insert_property
   below) rather than needing the real 250k+ row Tulsa County dataset -
   the code path (WHERE ILIKE ... ORDER BY ... LIMIT) is identical either
   way, and a live 250k-row fixture would make this suite slow and would
   require shipping real county PII into version control.
"""
import asyncio
import secrets

import pytest

import server
from db import get_cursor
from rate_limit import PerKeyRateLimitMiddleware

from conftest import insert_api_key, run_pipeline

INJECTION_PAYLOADS = [
    "'; DROP TABLE properties; --",
    "' OR '1'='1",
    "1' UNION SELECT * FROM api_keys--",
    "%'; DELETE FROM properties WHERE '1'='1",
]
INJECTION_IDS = ["drop-table", "or-1-equals-1", "union-select", "percent-delete"]


def insert_property(account_no=None, owner_name="Test Owner", property_address="1 Test St",
                     property_city="Tulsa", property_zip="74103", total_acct_value=100000,
                     sale_date=None, sale_price=None):
    if account_no is None:
        account_no = f"ACCT-{secrets.token_hex(6)}"
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO properties (account_no, owner_name, property_address, property_city,
                                     property_zip, total_acct_value, sale_date, sale_price)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (account_no, owner_name, property_address, property_city, property_zip,
             total_acct_value, sale_date, sale_price),
        )
    return account_no


def _property_count():
    with get_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM properties")
        return cur.fetchone()["n"]


# ---------------------------------------------------------------------------
# search_properties
# ---------------------------------------------------------------------------

class TestSearchProperties:
    def test_valid_partial_address_returns_matching_results(self):
        insert_property(account_no="ACCT-A", owner_name="Jane Doe", property_address="123 Main St")
        insert_property(account_no="ACCT-B", owner_name="John Roe", property_address="456 Elm St")

        result = server.search_properties(address="Main")

        assert result["count"] == 1
        assert result["results"][0]["account_no"] == "ACCT-A"
        assert result["results"][0]["owner_name"] == "Jane Doe"
        assert result["results"][0]["property_address"] == "123 Main St"
        # summary fields only - not the full row (get_property_details is the full-row tool)
        assert "legal_description" not in result["results"][0]

    def test_no_matches_returns_not_found_dict_not_a_crash(self):
        result = server.search_properties(address="Nonexistent Avenue")
        assert result == {"error": "No records found for address: Nonexistent Avenue"}

    def test_result_limit_of_20_is_enforced(self):
        for i in range(25):
            insert_property(account_no=f"ACCT-LIMIT-{i}", property_address=f"{i} Limit Test Rd")

        result = server.search_properties(address="Limit Test")

        assert result["count"] == 20
        assert len(result["results"]) == 20

    @pytest.mark.parametrize("payload", INJECTION_PAYLOADS, ids=INJECTION_IDS)
    def test_sql_injection_payloads_treated_as_literal_search_text(self, payload):
        insert_property(account_no="ACCT-SAFE", property_address="789 Safe St")

        result = server.search_properties(address=payload)

        assert result == {"error": f"No records found for address: {payload}"}
        assert _property_count() == 1  # the table (and its one real row) survived intact

    def test_empty_string_input_matches_everything_without_crashing(self):
        insert_property(account_no="ACCT-EMPTY", property_address="1 Empty Match St")
        result = server.search_properties(address="")
        assert result["count"] == 1  # '%' + '' + '%' = '%%', matches every address

    def test_excessively_long_input_handled_without_crashing(self):
        result = server.search_properties(address="x" * 10_000)
        assert result == {"error": f"No records found for address: {'x' * 10_000}"}


# ---------------------------------------------------------------------------
# lookup_owner
# ---------------------------------------------------------------------------

class TestLookupOwner:
    def test_valid_partial_owner_name_returns_matching_results(self):
        insert_property(account_no="ACCT-A", owner_name="Jane Smith", property_address="1 A St")
        insert_property(account_no="ACCT-B", owner_name="John Roe", property_address="2 B St")

        result = server.lookup_owner(owner_name="Smith")

        assert result["count"] == 1
        assert result["results"][0]["account_no"] == "ACCT-A"
        assert result["results"][0]["owner_name"] == "Jane Smith"

    def test_no_matches_returns_not_found_dict_not_a_crash(self):
        result = server.lookup_owner(owner_name="Nobody Real")
        assert result == {"error": "No records found for owner: Nobody Real"}

    def test_result_limit_of_20_is_enforced(self):
        for i in range(25):
            insert_property(account_no=f"ACCT-OWNER-LIMIT-{i}", owner_name=f"Limit Owner {i}")

        result = server.lookup_owner(owner_name="Limit Owner")

        assert result["count"] == 20
        assert len(result["results"]) == 20

    @pytest.mark.parametrize("payload", INJECTION_PAYLOADS, ids=INJECTION_IDS)
    def test_sql_injection_payloads_treated_as_literal_search_text(self, payload):
        insert_property(account_no="ACCT-SAFE", owner_name="Safe Owner")

        result = server.lookup_owner(owner_name=payload)

        assert result == {"error": f"No records found for owner: {payload}"}
        assert _property_count() == 1

    def test_empty_string_input_matches_everything_without_crashing(self):
        insert_property(account_no="ACCT-EMPTY", owner_name="Anyone")
        result = server.lookup_owner(owner_name="")
        assert result["count"] == 1

    def test_excessively_long_input_handled_without_crashing(self):
        result = server.lookup_owner(owner_name="y" * 10_000)
        assert result == {"error": f"No records found for owner: {'y' * 10_000}"}


# ---------------------------------------------------------------------------
# get_property_details
# ---------------------------------------------------------------------------

class TestGetPropertyDetails:
    def test_valid_account_no_returns_full_record(self):
        insert_property(account_no="ACCT-FULL", owner_name="Jane Doe", property_address="1 Full St")
        with get_cursor(commit=True) as cur:
            cur.execute("UPDATE properties SET legal_description = 'LOT 1 BLK 2' WHERE account_no = 'ACCT-FULL'")

        result = server.get_property_details(account_no="ACCT-FULL")

        assert result["account_no"] == "ACCT-FULL"
        assert result["owner_name"] == "Jane Doe"
        # unlike the two search tools, this is SELECT * - the full row, not just summary fields
        assert result["legal_description"] == "LOT 1 BLK 2"
        assert "id" in result

    def test_nonexistent_account_no_returns_not_found_dict_not_a_crash(self):
        result = server.get_property_details(account_no="ACCT-DOES-NOT-EXIST")
        assert result == {"error": "No property found with account_no: ACCT-DOES-NOT-EXIST"}

    @pytest.mark.parametrize("payload", INJECTION_PAYLOADS, ids=INJECTION_IDS)
    def test_malformed_account_no_handled_safely(self, payload):
        insert_property(account_no="ACCT-SAFE")

        result = server.get_property_details(account_no=payload)

        assert result == {"error": f"No property found with account_no: {payload}"}
        assert _property_count() == 1


# ---------------------------------------------------------------------------
# Usage logging across all three tools
# ---------------------------------------------------------------------------

def _fresh_key_and_limiter():
    raw_key, key_id = insert_api_key(monthly_quota=None)  # unlimited - quota shouldn't interfere here
    from auth import PostgresApiKeyVerifier
    token = asyncio.run(PostgresApiKeyVerifier().verify_token(raw_key))
    return token, key_id, PerKeyRateLimitMiddleware(default_limit=1000)


def _last_usage_row(key_id):
    with get_cursor() as cur:
        cur.execute(
            "SELECT tool_name, arguments, success, error_message FROM usage_log "
            "WHERE api_key_id = %s ORDER BY id DESC LIMIT 1",
            (key_id,),
        )
        return cur.fetchone()


class TestUsageLoggingAcrossTools:
    @pytest.mark.parametrize("tool_fn,tool_name,args", [
        (server.search_properties, "search_properties", {"address": "Main"}),
        (server.lookup_owner, "lookup_owner", {"owner_name": "Smith"}),
        (server.get_property_details, "get_property_details", {"account_no": "ACCT-X"}),
    ])
    def test_successful_call_logged_with_correct_tool_name_and_arguments(self, tool_fn, tool_name, args):
        insert_property(account_no="ACCT-X", owner_name="Smith Family", property_address="1 Main St")
        token, key_id, limiter = _fresh_key_and_limiter()

        run_pipeline(token, limiter, tool_name=tool_name, arguments=args, tool_fn=tool_fn)

        row = _last_usage_row(key_id)
        assert row["tool_name"] == tool_name
        assert row["arguments"] == args
        assert row["success"] is True
        assert row["error_message"] is None

    def test_no_results_call_still_logged_as_success_true(self):
        """The tools return an {"error": ...} DICT for "not found" - they
        don't raise a Python exception. UsageLoggingMiddleware's try/except
        only catches actual exceptions, so a clean no-match result falls
        through to the success=True branch, same as any other successful
        call. Confirmed here rather than assumed - this also means a
        "no results" search counts toward the caller's monthly quota."""
        token, key_id, limiter = _fresh_key_and_limiter()

        result = run_pipeline(
            token, limiter, tool_name="search_properties",
            arguments={"address": "Nonexistent Avenue"}, tool_fn=server.search_properties,
        )

        assert result == {"error": "No records found for address: Nonexistent Avenue"}
        row = _last_usage_row(key_id)
        assert row["success"] is True

    def test_exception_during_call_logged_as_success_false(self):
        """None of the three real tools raise in normal operation (they
        return error dicts instead - see the test above), so there's no
        natural way to exercise this path through them. This drives
        UsageLoggingMiddleware directly with a call_next that raises, to
        confirm the failure-logging branch itself actually works."""
        def _raise(**kwargs):
            raise RuntimeError("simulated DB failure")

        token, key_id, limiter = _fresh_key_and_limiter()

        with pytest.raises(RuntimeError):
            run_pipeline(token, limiter, tool_name="search_properties",
                         arguments={"address": "Main"}, tool_fn=_raise)

        row = _last_usage_row(key_id)
        assert row["success"] is False
        assert row["error_message"] == "simulated DB failure"
