"""Data layer / ingest.py tests.

Confirmed against the actual current implementation before writing
anything (a lot has changed elsewhere in the codebase since this file was
first built, and this file itself may have drifted from its original
design):

- No MAX_PAGES concept exists anywhere in ingest.py. Checkpointing is
  purely a JSON file (ingest_progress.json) storing next_offset and
  running counters; the fetch loop runs until INCOG returns an empty page.
- Retry/backoff is intact: 429/503 get linear backoff (30s * attempt) via
  an early continue before raise_for_status() is even called; any other
  RequestException (including raise_for_status() failing on a different
  status code) gets exponential backoff (2**attempt). After MAX_RETRIES,
  fetch_page returns None rather than raising or looping forever.
- upsert_record's ON CONFLICT clause originally refreshed only 6 of ~20
  columns on re-sync (owner_name, legal_description, sale_date,
  sale_price, total_acct_value, last_synced_at) - everything else
  (property_type, year_built, baths, gross_sf, total_imp_value, ...) was
  set on first insert only and never updated on a later re-sync even if
  the source data changed. Found by this test suite; fixed in ingest.py
  to refresh every column except account_no (the conflict key) and id
  (serial PK). test_rerun_with_changed_source_data_updates_every_field
  below sweeps every field so this can't regress silently again.

Side effect worth knowing about: importing ingest.py runs
logging.basicConfig(handlers=[logging.FileHandler("ingest.log"), ...]) at
module level, which touches ingest.log in the CWD. Harmless (gitignored)
but worth knowing why that file gets touched by running this suite.

DB-level tests use db.py's get_cursor() (the isolated tulsa_data_test DB,
same as every other module), not ingest.py's own psycopg2.connect() - the
two are equivalent as far as upsert_record/constraint testing is
concerned, since upsert_record just takes a cursor.
"""
from unittest.mock import MagicMock, patch

import psycopg2
import pytest
import requests

import ingest
from db import get_cursor


def _property_row(account_no):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM properties WHERE account_no = %s", (account_no,))
        return cur.fetchone()


def _property_count():
    with get_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM properties")
        return cur.fetchone()["n"]


def make_attrs(**overrides):
    attrs = {
        "AccountNo": "ACCT-1", "ACCT_NUM": None, "ParcelNo": "PARCEL-1",
        "Owner": "Jane Doe", "Address1": "1 Owner Ln", "City": "Tulsa", "State": "OK", "ZIPCode": "74103",
        "PropertyAddress": "123 Main St", "PropertyZIP": "74103", "PropertyCity": "Tulsa",
        "Legal": "LOT 1 BLK 2", "Neighborhood": "Downtown", "PropertyType": "Residential",
        "SaleDate": "06-15-2020", "SalePrice": 150000, "DeedType": "Warranty",
        "YearBuilt": 1985, "YearRemodeled": 2005, "Baths": 2.5, "Stories": 2,
        "GrossSF": 2200, "GrossAcre": 0.25,
        "TotalImpValue": 120000, "TotalLandValue": 30000, "TotalAcctValue": 150000,
    }
    attrs.update(overrides)
    return attrs


# ---------------------------------------------------------------------------
# clean_record
# ---------------------------------------------------------------------------

class TestCleanRecord:
    def test_missing_address_is_skipped(self):
        assert ingest.clean_record(make_attrs(PropertyAddress=None)) is None
        assert ingest.clean_record(make_attrs(PropertyAddress="")) is None

    def test_missing_owner_is_skipped(self):
        assert ingest.clean_record(make_attrs(Owner=None)) is None
        assert ingest.clean_record(make_attrs(Owner="")) is None

    def test_valid_record_maps_all_fields_correctly(self):
        record = ingest.clean_record(make_attrs())
        assert record["account_no"] == "ACCT-1"
        assert record["owner_name"] == "Jane Doe"
        assert record["property_address"] == "123 Main St"
        assert record["legal_description"] == "LOT 1 BLK 2"
        assert record["sale_date"].isoformat() == "2020-06-15"
        assert record["sale_price"] == 150000.0
        assert record["year_built"] == 1985
        assert record["total_acct_value"] == 150000.0

    def test_account_no_falls_back_to_acct_num(self):
        record = ingest.clean_record(make_attrs(AccountNo=None, ACCT_NUM="FALLBACK-ACCT"))
        assert record["account_no"] == "FALLBACK-ACCT"

    def test_value_fields_default_to_zero_when_missing(self):
        record = ingest.clean_record(make_attrs(TotalImpValue=None, TotalLandValue=None, TotalAcctValue=None))
        assert record["total_imp_value"] == 0
        assert record["total_land_value"] == 0
        assert record["total_acct_value"] == 0

    def test_other_numeric_fields_stay_none_when_missing_not_defaulted_to_zero(self):
        """Only the three value fields get a 0 default (matches schema.sql's
        DEFAULT 0 comment) - everything else genuinely stays NULL."""
        record = ingest.clean_record(make_attrs(
            YearBuilt=None, YearRemodeled=None, Baths=None, Stories=None,
            GrossSF=None, GrossAcre=None, SalePrice=None,
        ))
        assert record["year_built"] is None
        assert record["year_remodeled"] is None
        assert record["baths"] is None
        assert record["stories"] is None
        assert record["gross_sf"] is None
        assert record["gross_acre"] is None
        assert record["sale_price"] is None

    def test_malformed_numeric_fields_dont_crash(self):
        record = ingest.clean_record(make_attrs(YearBuilt="not-a-year", Baths="lots", GrossSF="???"))
        assert record["year_built"] is None
        assert record["baths"] is None
        assert record["gross_sf"] is None

    @pytest.mark.parametrize("legal,expected", [
        ("LOT 1 BLK 2", "LOT 1 BLK 2"),
        ("  LOT 1 BLK 2  \n", "LOT 1 BLK 2"),
        ("   ", None),
        ("", None),
        (None, None),
    ], ids=["clean", "surrounding-whitespace", "whitespace-only", "empty-string", "none"])
    def test_legal_description_whitespace_handling(self, legal, expected):
        record = ingest.clean_record(make_attrs(Legal=legal))
        assert record["legal_description"] == expected

    def test_owner_and_address_whitespace_stripped(self):
        record = ingest.clean_record(make_attrs(Owner="  Jane Doe  ", PropertyAddress="  123 Main St  "))
        assert record["owner_name"] == "Jane Doe"
        assert record["property_address"] == "123 Main St"

    def test_malformed_sale_date_returns_none(self):
        for bad_date in ["not-a-date", "2020-06-15", "13-40-2020", ""]:
            assert ingest.clean_record(make_attrs(SaleDate=bad_date))["sale_date"] is None


class TestSafeCoercionHelpers:
    def test_safe_int(self):
        assert ingest.safe_int("42") == 42
        assert ingest.safe_int(42) == 42
        assert ingest.safe_int(None) is None
        assert ingest.safe_int("not a number") is None
        assert ingest.safe_int([1, 2]) is None

    def test_safe_float(self):
        assert ingest.safe_float("42.5") == 42.5
        assert ingest.safe_float(None) is None
        assert ingest.safe_float("garbage") is None

    def test_safe_date(self):
        assert ingest.safe_date("06-15-2020").isoformat() == "2020-06-15"
        assert ingest.safe_date(None) is None
        assert ingest.safe_date("") is None
        assert ingest.safe_date("2020-06-15") is None  # wrong format (ISO, not MM-DD-YYYY)


# ---------------------------------------------------------------------------
# fetch_page: retry/backoff
# ---------------------------------------------------------------------------

def _ok_response(features):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"features": features}
    return resp


def _status_response(status_code):
    resp = MagicMock()
    resp.status_code = status_code
    return resp


class TestFetchPageRetryBackoff:
    def test_success_on_first_attempt_no_retry_no_sleep(self):
        with patch("ingest.requests.get", return_value=_ok_response([{"attributes": {}}])) as mock_get, \
             patch("ingest.time.sleep") as mock_sleep:
            result = ingest.fetch_page(0)

        assert result == [{"attributes": {}}]
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    def test_transient_failure_then_success_uses_exponential_backoff(self):
        with patch("ingest.requests.get", side_effect=[
                requests.exceptions.ConnectionError("boom"), _ok_response([{"attributes": {}}]),
             ]) as mock_get, \
             patch("ingest.time.sleep") as mock_sleep:
            result = ingest.fetch_page(0)

        assert result == [{"attributes": {}}]
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once_with(2)  # RETRY_BACKOFF_SECONDS ** 1

    def test_persistent_network_failure_exhausts_retries_and_returns_none(self):
        with patch("ingest.requests.get", side_effect=requests.exceptions.ConnectionError("boom")) as mock_get, \
             patch("ingest.time.sleep") as mock_sleep:
            result = ingest.fetch_page(0)

        assert result is None
        assert mock_get.call_count == ingest.MAX_RETRIES == 3
        assert mock_sleep.call_args_list == [((2,),), ((4,),), ((8,),)]  # 2**1, 2**2, 2**3

    def test_429_uses_linear_backoff_scaled_by_attempt(self):
        with patch("ingest.requests.get", return_value=_status_response(429)) as mock_get, \
             patch("ingest.time.sleep") as mock_sleep:
            result = ingest.fetch_page(0)

        assert result is None
        assert mock_get.call_count == 3
        assert mock_sleep.call_args_list == [((30,),), ((60,),), ((90,),)]  # 30*1, 30*2, 30*3

    def test_503_then_success_returns_features(self):
        with patch("ingest.requests.get", side_effect=[
                _status_response(503), _ok_response([{"attributes": {"x": 1}}]),
             ]), \
             patch("ingest.time.sleep") as mock_sleep:
            result = ingest.fetch_page(0)

        assert result == [{"attributes": {"x": 1}}]
        mock_sleep.assert_called_once_with(30)

    def test_non_transient_http_error_also_retries_with_exponential_backoff(self):
        """A plain non-429/503 error status (e.g. 500) isn't special-cased -
        raise_for_status() turns it into a RequestException, same path as a
        network failure."""
        error_response = MagicMock()
        error_response.status_code = 500
        error_response.raise_for_status.side_effect = requests.exceptions.HTTPError("500 server error")

        with patch("ingest.requests.get", return_value=error_response) as mock_get, \
             patch("ingest.time.sleep") as mock_sleep:
            result = ingest.fetch_page(0)

        assert result is None
        assert mock_get.call_count == 3
        assert mock_sleep.call_args_list == [((2,),), ((4,),), ((8,),)]


# ---------------------------------------------------------------------------
# Checkpointing
# ---------------------------------------------------------------------------

class TestCheckpointing:
    def test_load_checkpoint_returns_none_when_no_file_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest, "CHECKPOINT_FILE", str(tmp_path / "does_not_exist.json"))
        assert ingest.load_checkpoint() is None

    def test_save_then_load_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest, "CHECKPOINT_FILE", str(tmp_path / "checkpoint.json"))
        state = {
            "next_offset": 5000, "total_upserted": 4800, "total_skipped": 150,
            "total_failed_records": 2, "failed_offsets": [2000],
        }
        ingest.save_checkpoint(state)
        assert ingest.load_checkpoint() == state


# ---------------------------------------------------------------------------
# upsert_record: idempotency and constraint enforcement
# ---------------------------------------------------------------------------

class TestUpsertIdempotency:
    def test_new_record_is_inserted(self):
        record = ingest.clean_record(make_attrs(AccountNo="ACCT-NEW"))
        with get_cursor(commit=True) as cur:
            assert ingest.upsert_record(cur, record) is True
        assert _property_row("ACCT-NEW") is not None

    def test_rerunning_the_same_record_does_not_duplicate(self):
        record = ingest.clean_record(make_attrs(AccountNo="ACCT-DUP"))
        with get_cursor(commit=True) as cur:
            ingest.upsert_record(cur, record)
        with get_cursor(commit=True) as cur:
            ingest.upsert_record(cur, record)  # re-run on identical source data
        assert _property_count() == 1

    def test_rerun_with_changed_value_on_a_synced_field_updates_it(self):
        record1 = ingest.clean_record(make_attrs(AccountNo="ACCT-REVAL", TotalAcctValue=100000))
        with get_cursor(commit=True) as cur:
            ingest.upsert_record(cur, record1)

        record2 = ingest.clean_record(make_attrs(AccountNo="ACCT-REVAL", TotalAcctValue=175000))
        with get_cursor(commit=True) as cur:
            ingest.upsert_record(cur, record2)

        row = _property_row("ACCT-REVAL")
        assert row["total_acct_value"] == 175000

    def test_rerun_with_changed_source_data_updates_every_field(self):
        """Regression test for the ON CONFLICT gap where only 6 of ~20
        columns were refreshed on re-sync - see ingest.py's upsert_record.
        Changes every field that can legitimately change at the source
        (everything except account_no) to a different value and
        confirms each one individually lands, not just the handful that
        happened to already work. A future regression that drops any
        single column back out of the SET clause will fail this loop on
        that specific field, not just pass silently."""
        account_no = "ACCT-FULL-RESYNC"
        record1 = ingest.clean_record(make_attrs(AccountNo=account_no))
        with get_cursor(commit=True) as cur:
            ingest.upsert_record(cur, record1)
        first_synced_at = _property_row(account_no)["last_synced_at"]

        record2 = ingest.clean_record(make_attrs(
            AccountNo=account_no, ParcelNo="PARCEL-2",
            Owner="John Roe", Address1="2 New Owner Ln", City="Sand Springs", State="TX", ZIPCode="99999",
            PropertyAddress="456 Elm St", PropertyZIP="74104", PropertyCity="Broken Arrow",
            Legal="LOT 5 BLK 9", Neighborhood="Brookside", PropertyType="Commercial",
            SaleDate="01-01-2023", SalePrice=300000, DeedType="Quitclaim",
            YearBuilt=1999, YearRemodeled=2015, Baths=3.5, Stories=1,
            GrossSF=3000, GrossAcre=0.5,
            TotalImpValue=200000, TotalLandValue=50000, TotalAcctValue=250000,
        ))
        with get_cursor(commit=True) as cur:
            ingest.upsert_record(cur, record2)

        row = _property_row(account_no)
        for field, expected in record2.items():
            if field == "account_no":
                continue
            assert row[field] == expected, f"{field} did not refresh on re-sync (still {row[field]!r})"
        assert row["last_synced_at"] > first_synced_at

    def test_bad_record_savepoint_rollback_does_not_abort_the_whole_transaction(self):
        bad_record = ingest.clean_record(make_attrs(AccountNo="ACCT-BAD"))
        del bad_record["owner_name"]  # missing a %()s placeholder key -> KeyError inside cur.execute
        good_record = ingest.clean_record(make_attrs(AccountNo="ACCT-GOOD"))

        with get_cursor(commit=True) as cur:
            assert ingest.upsert_record(cur, bad_record) is False
            assert ingest.upsert_record(cur, good_record) is True  # same transaction, still works

        assert _property_row("ACCT-GOOD") is not None
        assert _property_row("ACCT-BAD") is None


class TestSchemaConstraintsEnforcedAtDbLevel:
    def test_owner_name_not_null_enforced_by_postgres(self):
        with pytest.raises(psycopg2.errors.NotNullViolation):
            with get_cursor(commit=True) as cur:
                cur.execute(
                    "INSERT INTO properties (account_no, owner_name, property_address) VALUES (%s, NULL, %s)",
                    ("ACCT-BADOWNER", "1 Main St"),
                )

    def test_property_address_not_null_enforced_by_postgres(self):
        with pytest.raises(psycopg2.errors.NotNullViolation):
            with get_cursor(commit=True) as cur:
                cur.execute(
                    "INSERT INTO properties (account_no, owner_name, property_address) VALUES (%s, %s, NULL)",
                    ("ACCT-BADADDR", "Jane Doe"),
                )

    def test_account_no_unique_enforced_by_postgres_not_just_the_upsert_logic(self):
        with get_cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO properties (account_no, owner_name, property_address) VALUES (%s, %s, %s)",
                ("ACCT-UNIQUE", "Jane Doe", "1 Main St"),
            )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            with get_cursor(commit=True) as cur:
                # a plain duplicate INSERT, deliberately bypassing upsert_record's
                # ON CONFLICT handling, to prove the DB itself is the backstop
                cur.execute(
                    "INSERT INTO properties (account_no, owner_name, property_address) VALUES (%s, %s, %s)",
                    ("ACCT-UNIQUE", "John Roe", "2 Elm St"),
                )
