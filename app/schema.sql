-- Supply Chain Shipment Analytics — normalized schema (3NF)
--
-- Conventions
--   * Date CHECKs use `date(x) IS x`, not `=`: date() returns NULL for garbage
--     input and a NULL CHECK result counts as a pass in SQL.
--   * Surrogate INTEGER primary keys; natural keys enforced with UNIQUE.
--   * Dates stored as ISO-8601 TEXT ('YYYY-MM-DD') so they sort and compare
--     lexically and map 1:1 to Postgres DATE during migration.
--   * Money stored as integer cents to avoid float rounding.
--   * Every FK is declared; PRAGMA foreign_keys=ON is set on every connection
--     (see app/db.py) because SQLite does not enforce FKs by default.

CREATE TABLE IF NOT EXISTS regions (
    region_id   INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS countries (
    country_id  INTEGER PRIMARY KEY,
    iso_code    TEXT    NOT NULL UNIQUE CHECK (length(iso_code) = 2 AND iso_code = upper(iso_code)),
    name        TEXT    NOT NULL,
    region_id   INTEGER NOT NULL REFERENCES regions(region_id)
);

CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id   INTEGER PRIMARY KEY,
    supplier_code TEXT    NOT NULL UNIQUE,
    name          TEXT    NOT NULL,
    country_id    INTEGER NOT NULL REFERENCES countries(country_id),
    tier          INTEGER NOT NULL CHECK (tier IN (1, 2, 3)),
    onboarded_on  TEXT    NOT NULL CHECK (date(onboarded_on) IS onboarded_on)
);

CREATE TABLE IF NOT EXISTS carriers (
    carrier_id  INTEGER PRIMARY KEY,
    scac_code   TEXT    NOT NULL UNIQUE CHECK (length(scac_code) BETWEEN 2 AND 4),
    name        TEXT    NOT NULL,
    mode        TEXT    NOT NULL CHECK (mode IN ('road', 'rail', 'ocean', 'air'))
);

CREATE TABLE IF NOT EXISTS warehouses (
    warehouse_id   INTEGER PRIMARY KEY,
    code           TEXT    NOT NULL UNIQUE,
    name           TEXT    NOT NULL,
    country_id     INTEGER NOT NULL REFERENCES countries(country_id)
);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id              INTEGER PRIMARY KEY,
    carrier_id               INTEGER NOT NULL REFERENCES carriers(carrier_id),
    tracking_number          TEXT    NOT NULL CHECK (length(tracking_number) BETWEEN 6 AND 40),
    supplier_id              INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    destination_warehouse_id INTEGER NOT NULL REFERENCES warehouses(warehouse_id),
    ship_date                TEXT    NOT NULL CHECK (date(ship_date) IS ship_date),
    promised_date            TEXT    NOT NULL CHECK (date(promised_date) IS promised_date),
    delivered_date           TEXT             CHECK (delivered_date IS NULL OR date(delivered_date) IS delivered_date),
    status                   TEXT    NOT NULL CHECK (status IN ('in_transit', 'delivered', 'exception', 'cancelled')),
    weight_kg                REAL    NOT NULL CHECK (weight_kg > 0),
    freight_cost_cents       INTEGER NOT NULL CHECK (freight_cost_cents >= 0),

    -- Tracking numbers are only unique *within* a carrier: two carriers can and
    -- do issue the same string. A global UNIQUE(tracking_number) would reject
    -- valid data; no constraint would let duplicates in. The composite key is
    -- the correct natural key.
    CONSTRAINT uq_shipments_carrier_tracking UNIQUE (carrier_id, tracking_number),

    CONSTRAINT ck_promised_after_ship   CHECK (promised_date >= ship_date),
    CONSTRAINT ck_delivered_after_ship  CHECK (delivered_date IS NULL OR delivered_date >= ship_date),
    -- delivered  <=> delivered_date present
    CONSTRAINT ck_delivered_status      CHECK ((status = 'delivered') = (delivered_date IS NOT NULL))
);

-- Access paths for the dashboard's filters and aggregations.
-- (carrier_id, tracking_number) is already indexed by its UNIQUE constraint.
CREATE INDEX IF NOT EXISTS ix_shipments_supplier_ship ON shipments (supplier_id, ship_date);
CREATE INDEX IF NOT EXISTS ix_shipments_ship_date     ON shipments (ship_date);
-- Open pipeline (in_transit/exception) is ~3% of rows; a partial covering index keeps
-- the overdue/exception counters fast without indexing the 96% 'delivered' majority.
CREATE INDEX IF NOT EXISTS ix_shipments_open ON shipments (status, promised_date)
    WHERE status IN ('in_transit', 'exception');
CREATE INDEX IF NOT EXISTS ix_shipments_warehouse     ON shipments (destination_warehouse_id);
CREATE INDEX IF NOT EXISTS ix_shipments_delivered     ON shipments (delivered_date) WHERE delivered_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_suppliers_country       ON suppliers (country_id);

-- Precomputed delivery-delay rollups (keyed by delivered_date).
-- Counters are additive, so any coarser grain is a SUM and stays exact.
-- Row counts are bounded by dimension cardinality (days x carriers,
-- months x suppliers), NOT by shipment volume. See docs/SCALING.md.
CREATE TABLE IF NOT EXISTS delay_metrics_carrier_daily (
    metric_date        TEXT    NOT NULL,             -- delivered_date
    carrier_id         INTEGER NOT NULL REFERENCES carriers(carrier_id),
    delivered_count    INTEGER NOT NULL,
    late_count         INTEGER NOT NULL,
    total_delay_days   INTEGER NOT NULL,             -- sum of positive delays only
    max_delay_days     INTEGER NOT NULL,
    PRIMARY KEY (metric_date, carrier_id)
);

CREATE TABLE IF NOT EXISTS delay_metrics_supplier_monthly (
    metric_month       TEXT    NOT NULL,             -- 'YYYY-MM' of delivered_date
    supplier_id        INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    delivered_count    INTEGER NOT NULL,
    late_count         INTEGER NOT NULL,
    total_delay_days   INTEGER NOT NULL,
    max_delay_days     INTEGER NOT NULL,
    PRIMARY KEY (metric_month, supplier_id)
);
CREATE INDEX IF NOT EXISTS ix_dmsm_supplier ON delay_metrics_supplier_monthly (supplier_id, metric_month);

CREATE TABLE IF NOT EXISTS metrics_refresh_log (
    refresh_id     INTEGER PRIMARY KEY,
    refreshed_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    from_date      TEXT,
    to_date        TEXT,
    rows_written   INTEGER NOT NULL
);
