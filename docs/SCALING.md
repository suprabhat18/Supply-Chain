# Production Scaling Plan: SQLite → Postgres and Precomputed Delay Metrics

**Status:** proposal · **Owner:** analytics platform · **Scope:** `shipment-analytics` service

## 1. Where we are

The service runs on a single SQLite file holding a normalized schema (`app/schema.sql`):
1,200 suppliers, 18 carriers, 12 warehouses, 150,000 shipments, 33 MB on disk (~220 B/row
including indexes). Delivery-delay analytics are served from two additive rollup tables that
are refreshed incrementally on every write that touches a `delivered_date`.

Measured on the seeded dataset (median of 5, warm cache, `scripts/seed.py` data):

| Query | Live @150k | Rollup @150k | Live @1.5M | Rollup @1.5M |
|---|---:|---:|---:|---:|
| Summary (all time) | 64–93 ms | 3–5 ms | 680 ms | 8 ms¹ |
| Delays by carrier (12 mo) | 97–122 ms | 2–4 ms | 1,074 ms | 2 ms |
| Monthly trend (all time) | 122–130 ms | 6 ms | 1,143 ms | 6 ms |
| Worst suppliers (12 mo) | 106–121 ms | 6 ms | 1,172 ms | 8 ms |
| Rollup rows | — | 51,245 | — | 60,786 |

¹ After replacing the full `status` index with a partial covering index on open shipments
(`ix_shipments_open`), the always-live open-pipeline counter went from 40 ms to 8 ms at 1.5M rows.

**Takeaways.** Live aggregation cost is linear in shipment volume (10× data ≈ 10× latency).
Rollup cost is flat because rollup size is bounded by *dimension* cardinality
(days × carriers, months × suppliers), not by shipment count: 10× more shipments grew the
rollups by only 19%. That property is the core of this plan. The database engine change
addresses concurrency and operations; the rollups address read latency.

## 2. When to migrate (triggers)

SQLite is the right choice today. We migrate when **any** of these becomes true:

| Trigger | Why SQLite stops fitting | Signal to watch |
|---|---|---|
| More than one app instance, or a write path outside this service | SQLite is a local file and allows a single writer at a time (WAL lets readers proceed) | Deploy topology; `SQLITE_BUSY` errors |
| Sustained ingest > ~200 writes/s, or bulk carrier-feed loads during business hours | Writer lock serializes ingest against rollup refresh | p95 `POST /shipments` latency, lock waits |
| Shipments > ~10M (≈2–3 GB) | Backups, `VACUUM`, and schema changes need downtime | File size, maintenance window length |
| HA / PITR / RPO requirements | No replication or point-in-time recovery | Ops/SLA requirements |
| Ad-hoc BI access by analysts | Needs network access, roles, read replicas | Requests for direct DB access |

At the current synthetic growth rate, data volume is not the first trigger. Concurrency and
operations are, so plan the migration around the second app instance.

## 3. Migration plan

### Phase 0: make the code dialect-portable (no behavior change). **Done**

Implemented: `app/db.py` provides a `Dialect` (delay, `GREATEST`/`MAX`, month bucket) and a
connection adapter with one API on both engines. `app/schema_postgres.sql` is the Phase 1 schema.
Integrity errors are classified by constraint name on both engines. `tests/test_cross_engine.py`
is the Phase 2 verification: same seed into both engines, every API response compared, run in CI
against Postgres 16. What remains is the operational part (Phases 2–3 on production data), plus
a connection pool (`psycopg_pool`) to replace connect-per-request.

The original analysis follows.

All SQL lives in `app/queries.py`, `app/metrics.py` and `app/main.py`. These are the
SQLite-specific constructs to replace:

| SQLite today | Postgres | Where |
|---|---|---|
| Dates as `TEXT` + `CHECK (date(x) IS x)` | native `DATE`; checks become unnecessary | schema |
| `julianday(a) - julianday(b)` | `a - b` (integer days for `DATE`) | `metrics.DELAY_EXPR` |
| scalar `MAX(x, 0)` | `GREATEST(x, 0)` | rollup aggregates |
| `substr(d, 1, 7)` month bucket | `date_trunc('month', d)::date` | trend, supplier rollup |
| `SUM(status = 'x')` | `COUNT(*) FILTER (WHERE status = 'x')` | summary |
| `REAL weight_kg` | `NUMERIC(10,2)` | schema |
| `PRAGMA foreign_keys/journal_mode` | not needed | `app/db.py` |
| IntegrityError message parsing for 409 | `e.diag.constraint_name == 'uq_shipments_carrier_tracking'` | `main._integrity_error` |
| `?` placeholders | `%s` (psycopg 3) | everywhere |

Do this by isolating the expressions behind a tiny dialect module (`DELAY_EXPR`, `MONTH_EXPR`,
`POS(x)`) and swapping `app/db.py` for a `psycopg_pool.ConnectionPool`. We don't need an ORM:
the queries are hand-tuned aggregates and should stay readable SQL. Adopt **Alembic** for
migrations from this point on.

The parity test suite (`tests/test_metrics.py`, rollup vs. live on identical inputs) becomes
the acceptance gate and must pass unchanged against both engines in CI.

### Phase 1: Postgres schema

```sql
CREATE TABLE shipments (
    shipment_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    carrier_id               INT    NOT NULL REFERENCES carriers,
    tracking_number          TEXT   NOT NULL CHECK (length(tracking_number) BETWEEN 6 AND 40),
    supplier_id              INT    NOT NULL REFERENCES suppliers,
    destination_warehouse_id INT    NOT NULL REFERENCES warehouses,
    ship_date                DATE   NOT NULL,
    promised_date            DATE   NOT NULL,
    delivered_date           DATE,
    status                   shipment_status NOT NULL,          -- ENUM
    weight_kg                NUMERIC(10,2) NOT NULL CHECK (weight_kg > 0),
    freight_cost_cents       BIGINT NOT NULL CHECK (freight_cost_cents >= 0),
    CONSTRAINT uq_shipments_carrier_tracking UNIQUE (carrier_id, tracking_number),
    CONSTRAINT ck_promised_after_ship  CHECK (promised_date >= ship_date),
    CONSTRAINT ck_delivered_after_ship CHECK (delivered_date IS NULL OR delivered_date >= ship_date),
    CONSTRAINT ck_delivered_status     CHECK ((status = 'delivered') = (delivered_date IS NOT NULL))
);
CREATE INDEX ix_shipments_supplier_ship ON shipments (supplier_id, ship_date);
CREATE INDEX ix_shipments_ship_date_brin ON shipments USING brin (ship_date);  -- append-mostly
CREATE INDEX ix_shipments_open ON shipments (status, promised_date)
    WHERE status IN ('in_transit', 'exception');
```

All integrity guarantees carry over 1:1. In particular, the composite
`(carrier_id, tracking_number)` key stays a database constraint, never an app-level check.
Ingestion from carrier feeds becomes idempotent with
`INSERT … ON CONFLICT ON CONSTRAINT uq_shipments_carrier_tracking DO UPDATE SET status = EXCLUDED.status, delivered_date = EXCLUDED.delivered_date`.

### Phase 2: data copy and verification

1. Create the schema on Postgres with FKs and CHECKs **enabled**. Any row SQLite accepted but
   Postgres rejects is a finding, not something to silence.
2. Export each table from SQLite in dependency order (regions → countries → suppliers /
   carriers / warehouses → shipments) and load with `COPY`. Keep surrogate IDs with
   `OVERRIDING SYSTEM VALUE`, then `setval` the identity sequences.
3. Rebuild rollups on Postgres from facts. Don't copy them; rebuilding exercises the refresh code.
4. Verify:
   - row counts per table;
   - per-month checksums: `count(*), sum(freight_cost_cents), count(DISTINCT (carrier_id, tracking_number))`;
   - every `/analytics/*` endpoint returns identical JSON from both backends over a fixed grid
     of date ranges (script reuses the parity-test ranges).

At 150k rows this takes seconds. At 10M rows `COPY` stays in the minutes range.

### Phase 3: cutover and rollback

1. Shadow reads: run the Postgres backend in parallel for a few days. Serve from SQLite, also
   query Postgres, and log any diffs.
2. Cutover: pause ingest (carrier feeds buffer upstream), take a final delta of rows with
   `shipment_id > last_copied` plus status updates since the copy, switch `DATABASE_URL`, resume.
   Expected write pause: under 5 minutes.
3. Rollback: keep the SQLite file and a reverse-sync script for 14 days. Since the API contract
   is unchanged, rollback is a config flip.

## 4. Precomputed delivery-delay metrics as volume grows

### 4.1 Design (implemented)

- **Grain.** `delay_metrics_carrier_daily (metric_date, carrier_id)` and
  `delay_metrics_supplier_monthly (metric_month, supplier_id)`, keyed by `delivered_date`.
  We first prototyped day × supplier × carrier and measured it: 139k rollup rows for 144k
  delivered shipments, almost no compression. Splitting by access path gave a 3× smaller
  rollup today, and the gap grows with volume because the rollup size stays bounded.
- **Additive counters only:** `delivered_count`, `late_count`, `total_delay_days`, `max_delay_days`.
  Every dashboard number (on-time rate, average delay, average days late when late, max) is
  derived from these, so any coarser roll-up is a plain `SUM`/`MAX` and stays exact.
- **Exactness contract.** Rollup and live paths share one delay definition (`DELAY_EXPR`) and
  are tested for equality. The supplier rollup can only answer whole-month windows. The API
  returns 422 for a partial-month window instead of silently returning approximate numbers,
  and the dashboard falls back to `source=live` for that case.
- **Freshness.** Writes that set or change `delivered_date` refresh the affected day (and its
  month) synchronously. The open-pipeline counters (in transit, overdue, exceptions) are always
  live, served by a partial covering index.

### 4.2 Evolution path

| Volume / load | Change |
|---|---|
| Now (≤1M, single writer) | Synchronous per-day refresh on write (current). |
| Bulk feeds / >50 writes/s | Stop refreshing inline. Writes append the touched date(s) to a `metrics_dirty_dates` table (Postgres: trigger on `shipments` for INSERT/UPDATE of `delivered_date`, recording both old and new dates). A worker drains it every 1–5 min. The dashboard shows `metrics_refreshed_at` (already exposed on `/health`). |
| Postgres | Replace delete-and-reinsert with `INSERT … ON CONFLICT (metric_date, carrier_id) DO UPDATE` per dirty date, so readers never see a gap. Use plain tables, not materialized views: `REFRESH MATERIALIZED VIEW [CONCURRENTLY]` always recomputes everything, which loses the bounded-cost property. |
| Correctness at scale | Nightly reconciliation job: recompute the last 35 days live, diff against the rollup, alert on any drift, auto-repair. Also covers bugs and manual data fixes made outside the API. |
| New metric: percentiles (p50/p95 delay) | Percentiles aren't additive. Add a delay histogram per grain (`delay_bucket` 0,1,2,3–5,6–10,11–20,21+ days → count), which stays additive and gives bucket-accurate percentiles. Use t-digest only if finer precision is required. |
| New dimension (lane, warehouse) | Add a new rollup keyed by that dimension rather than widening an existing one. Each rollup serves one access path, and its size stays bounded. |
| >100M shipments | Partition `shipments` by `ship_date` (monthly). **Caveat:** in Postgres a UNIQUE constraint on a partitioned table must include the partition key, so `(carrier_id, tracking_number)` can no longer be enforced on the parent. Keep it enforced via an unpartitioned `shipment_keys (carrier_id, tracking_number) PRIMARY KEY` table written in the same transaction. Don't weaken the integrity guarantee to get partitioning. |

### 4.3 Read path at scale

- Route dashboard reads to a read replica. Rollups replicate like any table.
- Set `statement_timeout` (e.g. 2 s) on `source=live` queries and expose `live` only to
  internal users. The public dashboard uses rollups.
- Use `/shipments` keyset pagination (`after_id`, already implemented) everywhere. Offset
  pagination degrades linearly with depth.
- Put PgBouncer in transaction mode in front of Postgres once there are more than ~4 app workers.

## 5. Capacity estimate

At ~220 B/row including indexes (measured on SQLite; Postgres will be roughly 1.3–1.6× due to
tuple headers):

| Shipments | Fact table + indexes | Rollups | Notes |
|---:|---:|---:|---|
| 150k | 33 MB | 51k rows | today |
| 1.5M | 310 MB | 61k rows | measured |
| 10M | ~2–3 GB | ~70k rows | migrate before here |
| 100M | ~25–35 GB | ~100k rows | partition + key registry (§4.2) |

## 6. Risks

| Risk | Mitigation |
|---|---|
| Behavior drift between engines (date math, rounding) | Parity suite runs against both engines; shadow-read diffing before cutover |
| Data that SQLite accepted but Postgres rejects | Constraints on during load; treat rejections as data-quality findings |
| Rollup staleness after moving to async refresh | Show `metrics_refreshed_at` in the UI; reconciliation job; dirty-date queue depth alert |
| Tracking-number reuse by a carrier over years | Monitor ON CONFLICT hits per carrier. If a carrier recycles numbers, extend the natural key with a recycle epoch (e.g. `ship_year`) rather than dropping the constraint |
