import argparse
import json
import logging
import os
import time
from datetime import datetime

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://map11.incog.org/arcgis11wa/rest/services/Parcels_TulsaCo/FeatureServer/0/query"
PAGE_SIZE = 1000

# --- DB connection - loaded from .env (see .env.example if you need to recreate it) ---
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_HOST = os.environ["DB_HOST"]

# --- Politeness / resilience settings ---
REQUEST_DELAY_SECONDS = 0.75          # pause between page requests
BUSINESS_HOUR_START = 8               # 8am local time
BUSINESS_HOUR_END = 16                # 4pm local time
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2             # exponential: 2s, 4s, 8s
RATE_LIMIT_BACKOFF_SECONDS = 30       # base backoff for 429/503, scaled by attempt

CHECKPOINT_FILE = "ingest_progress.json"
INCOG_REPORTED_TOTAL = 283996         # from returnCountOnly=true, checked 2026-08-13

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("ingest.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def wait_if_business_hours():
    """Pause until 4pm if we're currently inside the 8am-4pm local window."""
    now = datetime.now()
    if BUSINESS_HOUR_START <= now.hour < BUSINESS_HOUR_END:
        resume_at = now.replace(hour=BUSINESS_HOUR_END, minute=0, second=0, microsecond=0)
        wait_seconds = (resume_at - now).total_seconds()
        log.info(
            f"Within business hours ({BUSINESS_HOUR_START}am-{BUSINESS_HOUR_END - 12}pm). "
            f"Pausing until {resume_at.strftime('%H:%M')} ({wait_seconds / 60:.1f} min)..."
        )
        time.sleep(max(wait_seconds, 0))


def fetch_page(offset):
    """Fetch one page of features, retrying on failure. Returns a list of
    features, or None if the page could not be fetched after MAX_RETRIES."""
    params = {
        "where": "1=1",
        "outFields": "*",
        "f": "json",
        "resultOffset": offset,
        "resultRecordCount": PAGE_SIZE,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(BASE_URL, params=params, timeout=30)
            if response.status_code in (429, 503):
                wait = RATE_LIMIT_BACKOFF_SECONDS * attempt
                log.warning(
                    f"Got HTTP {response.status_code} at offset {offset} "
                    f"(attempt {attempt}/{MAX_RETRIES}). Backing off {wait}s..."
                )
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json().get("features", [])
        except requests.exceptions.RequestException as e:
            wait = RETRY_BACKOFF_SECONDS ** attempt
            log.warning(
                f"Request failed at offset {offset} (attempt {attempt}/{MAX_RETRIES}): {e}. "
                f"Retrying in {wait}s..."
            )
            time.sleep(wait)

    log.error(f"Offset {offset} failed after {MAX_RETRIES} attempts - skipping this page.")
    return None


def safe_int(value):
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def safe_float(value):
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def safe_date(value):
    """INCOG returns SaleDate as a 'MM-DD-YYYY' string, not a real date/epoch field."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%m-%d-%Y").date()
    except (ValueError, TypeError):
        return None


def clean_record(attrs):
    """Apply the same null-safety rules from pull_sample.py, plus defensive
    type coercion so a malformed numeric field can't crash the insert."""
    address = attrs.get("PropertyAddress")
    owner = attrs.get("Owner")

    if not address or not owner:
        return None  # skip - matches pull_sample.py logic

    return {
        "account_no": attrs.get("AccountNo") or attrs.get("ACCT_NUM"),
        "parcel_no": attrs.get("ParcelNo"),
        "owner_name": owner.strip(),
        "owner_address1": attrs.get("Address1"),
        "owner_city": attrs.get("City"),
        "owner_state": attrs.get("State"),
        "owner_zip": attrs.get("ZIPCode"),
        "property_address": address.strip(),
        "property_zip": attrs.get("PropertyZIP"),
        "property_city": attrs.get("PropertyCity"),
        "legal_description": (attrs.get("Legal") or "").strip() or None,
        "neighborhood": attrs.get("Neighborhood"),
        "property_type": attrs.get("PropertyType"),
        "sale_date": safe_date(attrs.get("SaleDate")),
        "sale_price": safe_float(attrs.get("SalePrice")),
        "deed_type": attrs.get("DeedType"),
        "year_built": safe_int(attrs.get("YearBuilt")),
        "year_remodeled": safe_int(attrs.get("YearRemodeled")),
        "baths": safe_float(attrs.get("Baths")),
        "stories": safe_float(attrs.get("Stories")),
        "gross_sf": safe_int(attrs.get("GrossSF")),
        "gross_acre": safe_float(attrs.get("GrossAcre")),
        "total_imp_value": safe_float(attrs.get("TotalImpValue")) or 0,
        "total_land_value": safe_float(attrs.get("TotalLandValue")) or 0,
        "total_acct_value": safe_float(attrs.get("TotalAcctValue")) or 0,
    }


def upsert_record(cur, r):
    """Insert new, or update existing record matched by account_no. Uses a
    savepoint so one bad record can't abort the whole page's transaction."""
    cur.execute("SAVEPOINT record_sp")
    try:
        cur.execute("""
            INSERT INTO properties (
                account_no, parcel_no, owner_name, owner_address1, owner_city, owner_state, owner_zip,
                property_address, property_zip, property_city, legal_description, neighborhood, property_type,
                sale_date, sale_price, deed_type, year_built, year_remodeled, baths, stories, gross_sf, gross_acre,
                total_imp_value, total_land_value, total_acct_value, last_synced_at
            ) VALUES (
                %(account_no)s, %(parcel_no)s, %(owner_name)s, %(owner_address1)s, %(owner_city)s, %(owner_state)s, %(owner_zip)s,
                %(property_address)s, %(property_zip)s, %(property_city)s, %(legal_description)s, %(neighborhood)s, %(property_type)s,
                %(sale_date)s, %(sale_price)s, %(deed_type)s, %(year_built)s, %(year_remodeled)s, %(baths)s, %(stories)s, %(gross_sf)s, %(gross_acre)s,
                %(total_imp_value)s, %(total_land_value)s, %(total_acct_value)s, NOW()
            )
            ON CONFLICT (account_no) DO UPDATE SET
                parcel_no = EXCLUDED.parcel_no,
                owner_name = EXCLUDED.owner_name,
                owner_address1 = EXCLUDED.owner_address1,
                owner_city = EXCLUDED.owner_city,
                owner_state = EXCLUDED.owner_state,
                owner_zip = EXCLUDED.owner_zip,
                property_address = EXCLUDED.property_address,
                property_zip = EXCLUDED.property_zip,
                property_city = EXCLUDED.property_city,
                legal_description = EXCLUDED.legal_description,
                neighborhood = EXCLUDED.neighborhood,
                property_type = EXCLUDED.property_type,
                sale_date = EXCLUDED.sale_date,
                sale_price = EXCLUDED.sale_price,
                deed_type = EXCLUDED.deed_type,
                year_built = EXCLUDED.year_built,
                year_remodeled = EXCLUDED.year_remodeled,
                baths = EXCLUDED.baths,
                stories = EXCLUDED.stories,
                gross_sf = EXCLUDED.gross_sf,
                gross_acre = EXCLUDED.gross_acre,
                total_imp_value = EXCLUDED.total_imp_value,
                total_land_value = EXCLUDED.total_land_value,
                total_acct_value = EXCLUDED.total_acct_value,
                last_synced_at = NOW()
        """, r)
        cur.execute("RELEASE SAVEPOINT record_sp")
        return True
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT record_sp")
        log.error(f"Failed to upsert account_no={r.get('account_no')!r}: {e}")
        return False


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return None


def save_checkpoint(state):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--restart", action="store_true",
        help="Ignore any existing checkpoint and start over from offset 0",
    )
    args = parser.parse_args()

    state = None if args.restart else load_checkpoint()
    if state:
        offset = state["next_offset"]
        total_upserted = state["total_upserted"]
        total_skipped = state["total_skipped"]
        total_failed_records = state["total_failed_records"]
        failed_offsets = state["failed_offsets"]
        log.info(f"Resuming from checkpoint at offset {offset} (upserted so far: {total_upserted})")
    else:
        if args.restart and os.path.exists(CHECKPOINT_FILE):
            log.info("--restart passed: ignoring existing checkpoint, starting from offset 0")
        offset = 0
        total_upserted = 0
        total_skipped = 0
        total_failed_records = 0
        failed_offsets = []

    conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD, host=DB_HOST)
    cur = conn.cursor()

    while True:
        wait_if_business_hours()

        log.info(f"Fetching offset {offset}...")
        features = fetch_page(offset)

        if features is None:
            failed_offsets.append(offset)
            offset += PAGE_SIZE
            save_checkpoint({
                "next_offset": offset,
                "total_upserted": total_upserted,
                "total_skipped": total_skipped,
                "total_failed_records": total_failed_records,
                "failed_offsets": failed_offsets,
            })
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if not features:
            log.info("No more features returned - ingestion complete.")
            break

        for feature in features:
            record = clean_record(feature["attributes"])
            if record is None:
                total_skipped += 1
                continue
            if upsert_record(cur, record):
                total_upserted += 1
            else:
                total_failed_records += 1

        conn.commit()
        offset += PAGE_SIZE
        save_checkpoint({
            "next_offset": offset,
            "total_upserted": total_upserted,
            "total_skipped": total_skipped,
            "total_failed_records": total_failed_records,
            "failed_offsets": failed_offsets,
        })
        time.sleep(REQUEST_DELAY_SECONDS)

    cur.execute("SELECT COUNT(*) FROM properties")
    final_row_count = cur.fetchone()[0]

    cur.close()
    conn.close()

    log.info("=== Ingestion Summary ===")
    log.info(f"Total pages processed: {offset // PAGE_SIZE}")
    log.info(f"Total upserted: {total_upserted}")
    log.info(f"Total skipped (missing address/owner): {total_skipped}")
    log.info(f"Total failed records (bad data / DB error): {total_failed_records}")
    log.info(f"Failed offsets (network/server errors - retry manually): {failed_offsets or 'none'}")
    log.info(f"Final row count in properties table: {final_row_count} (INCOG reported total: {INCOG_REPORTED_TOTAL})")

    if not failed_offsets:
        if os.path.exists(CHECKPOINT_FILE):
            os.remove(CHECKPOINT_FILE)
        log.info("No failed offsets - checkpoint file removed. Run is complete.")
    else:
        log.info(f"Checkpoint file kept ({CHECKPOINT_FILE}) - failed offsets are recorded there for manual retry.")


if __name__ == "__main__":
    main()
