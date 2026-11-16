# Supply Chain Shipment Analytics

Python · SQLite · FastAPI. A normalized logistics store (1,200 suppliers, 150,000 shipments)
with delivery-delay analytics, a dashboard, and a scaling plan for moving to Postgres.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m scripts.seed              # builds data/shipments.db (~10 s)
.venv/bin/uvicorn app.main:app --reload       # dashboard: http://localhost:8000  API docs: /docs
.venv/bin/python -m pytest -q                 # 40 tests
```

`scripts.seed` takes `--suppliers`, `--shipments`, `--seed` and `--db`. Set `SHIPMENTS_DB` to
point the API at another file.

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
