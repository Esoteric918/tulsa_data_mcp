-- Tulsa County Parcels - core property table
-- Source: INCOG ArcGIS FeatureServer (Parcels_TulsaCo)

CREATE TABLE properties (
    id              SERIAL PRIMARY KEY,
    account_no      TEXT UNIQUE,        -- ACCT_NUM / AccountNo from source
    parcel_no       TEXT,               -- ParcelNo from source
    owner_name      TEXT NOT NULL,      -- Owner (required - we filter out records missing this)
    owner_address1  TEXT,               -- Address1
    owner_city      TEXT,               -- City
    owner_state     TEXT,               -- State
    owner_zip       TEXT,               -- ZIPCode

    property_address TEXT NOT NULL,     -- PropertyAddress (required - we filter out records missing this)
    property_zip      TEXT,             -- PropertyZIP
    property_city     TEXT,             -- PropertyCity

    legal_description TEXT,             -- Legal
    neighborhood       TEXT,            -- Neighborhood
    property_type       TEXT,           -- PropertyType

    -- Sale info
    sale_date       DATE,               -- SaleDate
    sale_price      NUMERIC,            -- SalePrice
    deed_type       TEXT,               -- DeedType

    -- Physical characteristics
    year_built      INTEGER,            -- YearBuilt
    year_remodeled  INTEGER,            -- YearRemodeled
    baths           NUMERIC,            -- Baths
    stories         NUMERIC,            -- Stories
    gross_sf        INTEGER,            -- GrossSF
    gross_acre      NUMERIC,            -- GrossAcre

    -- Value fields (default to 0 rather than NULL, per our null-safety approach)
    total_imp_value   NUMERIC DEFAULT 0,   -- TotalImpValue
    total_land_value  NUMERIC DEFAULT 0,   -- TotalLandValue
    total_acct_value  NUMERIC DEFAULT 0,   -- TotalAcctValue

    -- Bookkeeping
    last_synced_at  TIMESTAMP DEFAULT NOW()
);

-- Index for the lookups your MCP tool will actually do
CREATE INDEX idx_properties_address ON properties (property_address);
CREATE INDEX idx_properties_owner ON properties (owner_name);

-- Tax delinquency / resale tracking
-- Oklahoma is a tax DEED state (not lien certificates) - after 3 years
-- delinquent, county auctions the property itself via tax resale (68 O.S. § 3105+)

CREATE TABLE tax_delinquency (
    id                  SERIAL PRIMARY KEY,
    account_no          TEXT REFERENCES properties(account_no),
    is_delinquent       BOOLEAN DEFAULT FALSE,
    delinquent_since     DATE,           -- first date taxes went unpaid
    years_delinquent     NUMERIC,        -- calculated - flags 3+ as resale-eligible
    amount_owed          NUMERIC DEFAULT 0,
    resale_eligible      BOOLEAN DEFAULT FALSE,  -- true once 3+ years delinquent
    resale_auction_date  DATE,           -- scheduled county tax resale date, if known
    status                TEXT,          -- e.g. "delinquent", "notice sent", "scheduled for resale", "redeemed"
    last_checked_at       TIMESTAMP DEFAULT NOW(),

    UNIQUE(account_no)
);

CREATE INDEX idx_tax_delinquency_eligible ON tax_delinquency (resale_eligible);

-- API keys for MCP server auth
-- Only the SHA-256 hash of the raw key is ever stored - the raw key is
-- shown once at generation time (see generate_api_key.py) and never again.

CREATE TABLE api_keys (
    id                    SERIAL PRIMARY KEY,
    key_hash              TEXT UNIQUE NOT NULL,
    owner_name            TEXT NOT NULL,
    created_at            TIMESTAMP DEFAULT NOW(),
    revoked_at            TIMESTAMP,
    expires_at            TIMESTAMP,       -- NULL = does not expire
    monthly_quota         INTEGER,         -- NULL = unlimited; enforced against usage_log call counts
    rate_limit_per_minute INTEGER,         -- NULL = falls back to DEFAULT_RATE_LIMIT_PER_MINUTE
    stripe_customer_id    TEXT,
    stripe_subscription_id TEXT,
    plan_tier             TEXT,            -- 'manual' or 'agent', from the Stripe Price metadata

    -- Set when we've emailed the customer about a failed renewal charge;
    -- cleared once the subscription goes active again. Lets the past_due
    -- webhook branch send the notice exactly once per failure episode
    -- instead of re-sending on every redelivered/duplicate webhook while
    -- still past_due.
    payment_failed_notified_at TIMESTAMP
);

CREATE INDEX idx_api_keys_stripe_subscription ON api_keys (stripe_subscription_id);
CREATE INDEX idx_api_keys_stripe_customer ON api_keys (stripe_customer_id);

-- Per-CUSTOMER tier-change cooldown state. Deliberately keyed on
-- stripe_customer_id rather than living on the api_keys row: a canceled
-- subscription that gets immediately resubscribed produces a brand new
-- api_keys row (see handle_checkout_completed), and the customer id is the
-- one thing that survives that churn.
--
-- tier_change_cooldown_until is a plain wall-clock deadline we compute and
-- store ourselves (now + the price's own billing interval) - deliberately
-- NOT derived from Stripe's current_period_start. Stripe mints a brand new
-- current_period_start for every new subscription, including one created
-- seconds after a cancellation, so comparing against that value (even if
-- stored per-customer) would let a cancel+resubscribe cycle silently grant
-- a fresh tier-change window every time. See handle_subscription_updated
-- in billing.py.

CREATE TABLE customer_cooldowns (
    stripe_customer_id          TEXT PRIMARY KEY,
    tier_change_cooldown_until  TIMESTAMP
);

-- Idempotency guard for Stripe webhooks: Stripe can and does deliver the
-- same event more than once (retries). Every webhook handler must insert
-- the event id here (ON CONFLICT DO NOTHING) before doing any real work,
-- and skip processing entirely if the row already existed.

CREATE TABLE stripe_events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    processed_at TIMESTAMP DEFAULT NOW()
);

-- Per-call usage log, for billing and debugging

CREATE TABLE usage_log (
    id             SERIAL PRIMARY KEY,
    api_key_id     INTEGER REFERENCES api_keys(id),
    tool_name      TEXT NOT NULL,
    arguments      JSONB,
    called_at      TIMESTAMP DEFAULT NOW(),
    success        BOOLEAN,
    error_message  TEXT
);

CREATE INDEX idx_usage_log_api_key ON usage_log (api_key_id);
CREATE INDEX idx_usage_log_called_at ON usage_log (called_at);