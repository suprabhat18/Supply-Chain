# Supply Chain Shipment Analytics

[![tests](https://github.com/suprabhat18/Supply-Chain/actions/workflows/ci.yml/badge.svg)](https://github.com/suprabhat18/Supply-Chain/actions/workflows/ci.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab)
![SQLite | Postgres 16](https://img.shields.io/badge/db-SQLite%20%7C%20Postgres%2016-336791)
![FastAPI](https://img.shields.io/badge/api-FastAPI-009688)

Python · SQLite / Postgres · FastAPI. A normalized logistics store (1,200 suppliers,
150,000 shipments) with delivery-delay analytics, a dashboard, and a tested path from SQLite
to Postgres.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/dashboard-dark.png">
  <img alt="Dashboard: KPI tiles, monthly on-time rate line chart, on-time rate by carrier bar chart" src="docs/images/dashboard-light.png">
</picture>

**Highlights**
- **Integrity in the database, not just the API:** `UNIQUE (carrier_id, tracking_number)`
  allows the same tracking number across carriers but never within one. The seed reuses about
  2,700 tracking numbers across carriers to prove it. FKs and CHECKs are enforced on both engines.
- **Precomputed delay metrics:** queries take 2–8 ms whether there are 150k or 1.5M shipments,
  versus about 100 ms → 1.1 s for live scans. Rollup size depends on days × carriers, not on
  shipment count. Tests assert rollup results equal live results exactly.
- **Same code on SQLite and Postgres:** CI seeds identical data into both and asserts that every
  API response matches (102 tests against Postgres 16).
- **Scaling plan** with measured numbers: [docs/SCALING.md](docs/SCALING.md).

## Live demo

Deployed on Render's free tier (`render.yaml`). The 150k-shipment SQLite database is built
during deploy, and the deployment is read-only (`DEMO_READ_ONLY=1`: write endpoints return
403). Free services sleep after 15 minutes idle, so the first visit afterwards takes about a minute.

To deploy your own copy: Render dashboard → **New → Blueprint** → select this repo → **Apply**.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m scripts.seed              # builds data/shipments.db (~10 s)
.venv/bin/uvicorn app.main:app --reload       # dashboard: http://localhost:8000  API docs: /docs
.venv/bin/python -m pytest -q                 # SQLite; see below for Postgres
```

`scripts.seed` takes `--suppliers`, `--shipments`, `--seed` and `--db`.

### Running on Postgres

The same code runs on SQLite or Postgres; `DATABASE_URL` selects the engine (a file path or a
`postgresql://` URL). The Postgres schema is `app/schema_postgres.sql`.

```bash
.venv/bin/python -m scripts.seed --db postgresql://user:pass@localhost/shipments
DATABASE_URL=postgresql://user:pass@localhost/shipments .venv/bin/uvicorn app.main:app
```

Tests run on SQLite by default. With `TEST_DATABASE_URL` set to a Postgres server (user needs
`CREATEDB`), every test also runs on Postgres, plus `tests/test_cross_engine.py`, which seeds
identical data into both engines and asserts every API response is identical. CI
(`.github/workflows/ci.yml`) does this against Postgres 16 on every push.

## Schema (`app/schema.sql`)

```
regions ─< countries ─< suppliers ─< shipments >─ carriers
                    └─< warehouses ─<─┘
delay_metrics_carrier_daily (metric_date, carrier_id)       ← precomputed
delay_metrics_supplier_monthly (metric_month, supplier_id)  ← precomputed
```

- **3NF.** Every attribute lives in exactly one place: country → region, supplier → country,
  shipment → supplier/carrier/warehouse by FK. Surrogate keys, with natural keys
  (`scac_code`, `supplier_code`, warehouse `code`, `iso_code`) enforced `UNIQUE`.
- **`UNIQUE (carrier_id, tracking_number)`.** Tracking numbers are unique only within a carrier.
  A global unique key would reject real data; no key would let duplicates in. The seed
  deliberately reuses ~2,700 tracking numbers across carriers to exercise this.
  `GET /shipments/lookup` therefore requires both carrier and tracking number, and duplicate
  ingestion returns **409**.
- **CHECK constraints** on dates (valid ISO, promised ≥ ship, delivered ≥ ship), status enum,
  status ↔ `delivered_date` consistency, and positive weight and cost. FKs are enforced on every
  connection (`PRAGMA foreign_keys = ON`).
- **Indexes** match the access paths, including a partial covering index for the open pipeline.

## Delay metrics

`delay_days = delivered_date − promised_date`. A shipment is late if `delay_days > 0`.
Every analytics endpoint takes `source=rollup` (default, precomputed) or `source=live`
(scans `shipments`). Tests assert the two paths return identical numbers. Writes that touch
`delivered_date` refresh the affected day and month in the rollups. `POST /admin/refresh-metrics`
rebuilds any window.

| | live @150k | rollup @150k | live @1.5M | rollup @1.5M |
|---|---:|---:|---:|---:|
| delays by carrier | ~100 ms | ~3 ms | ~1,070 ms | ~2 ms |

## API

| Method | Path | |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/health` | Row count and last metrics refresh |
| GET | `/carriers`, `/suppliers` | Reference data (filters: country, tier, q) |
| GET | `/suppliers/{id}` | Supplier scorecard with monthly delay trend |
| GET | `/shipments` | Filter by supplier/carrier/status/ship date/`late_only`; keyset pagination via `after_id` |
| GET | `/shipments/lookup?carrier_scac=&tracking_number=` | Natural-key lookup |
| GET | `/shipments/{id}` | |
| POST | `/shipments` | Ingest by natural keys; 409 on carrier+tracking duplicate, 422 on validation |
| PATCH | `/shipments/{id}` | Status / delivery update |
| GET | `/analytics/summary` | Delivered, on-time rate, delay stats, open pipeline |
| GET | `/analytics/delays/by-carrier` | Worst first |
| GET | `/analytics/delays/by-supplier` | `order=worst|best`, `min_delivered` |
| GET | `/analytics/delays/trend` | Monthly, optional `carrier_id` |
| POST | `/admin/refresh-metrics` | Rebuild rollups for a window |

## Scaling

See [docs/SCALING.md](docs/SCALING.md): triggers for leaving SQLite, a phased Postgres
migration with verification and rollback, the incremental-metrics roadmap (dirty-date queue,
upserts, reconciliation, histograms for percentiles), and the partitioning caveat for the
composite unique key.
