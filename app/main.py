"""FastAPI service for the shipment analytics dashboard.

    uvicorn app.main:app --reload
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from . import queries
from .db import IntegrityError, connect, transaction, violation
from .metrics import delay_expr, refresh_delay_metrics
from .models import Shipment, ShipmentCreate, ShipmentPage, ShipmentStatusUpdate

app = FastAPI(
    title="Supply Chain Shipment Analytics",
    version="1.0.0",
    description="Shipment search, ingestion and delivery-delay analytics over a normalized SQLite store.",
)
STATIC = Path(__file__).with_name("static")

Source = Literal["rollup", "live"]
SOURCE_Q = Query("rollup", description="`rollup` = precomputed metrics (fast); `live` = scan shipments (always fresh)")
FROM_Q = Query(None, description="Inclusive lower bound on delivered_date")
TO_Q = Query(None, description="Inclusive upper bound on delivered_date")


def get_conn() -> Iterator[Any]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _check_range(date_from: date | None, date_to: date | None) -> None:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")


def shipment_select(conn: Any) -> str:
    return f"""
    SELECT s.shipment_id, c.scac_code AS carrier_scac, s.tracking_number, s.supplier_id,
           sp.supplier_code, w.code AS warehouse_code, s.ship_date, s.promised_date,
           s.delivered_date, s.status,
           CASE WHEN s.delivered_date IS NOT NULL THEN {delay_expr(conn.dialect)} END AS delay_days,
           s.weight_kg, s.freight_cost_cents
    FROM shipments s
    JOIN carriers c   ON c.carrier_id = s.carrier_id
    JOIN suppliers sp ON sp.supplier_id = s.supplier_id
    JOIN warehouses w ON w.warehouse_id = s.destination_warehouse_id
"""


# ------------------------------------------------------------------ dashboard & health
@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health(conn: Any = Depends(get_conn)) -> dict[str, Any]:
    last = conn.execute("SELECT MAX(refreshed_at) FROM metrics_refresh_log").fetchone()[0]
    return {"status": "ok", "shipments": conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0],
            "metrics_refreshed_at": last}


# ------------------------------------------------------------------ reference data
@app.get("/carriers")
def list_carriers(conn: Any = Depends(get_conn)) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT carrier_id, scac_code, name, mode FROM carriers ORDER BY name")]


@app.get("/suppliers")
def list_suppliers(
    country: str | None = Query(None, min_length=2, max_length=2, description="ISO country code"),
    tier: int | None = Query(None, ge=1, le=3),
    q: str | None = Query(None, description="Substring match on name or code"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: Any = Depends(get_conn),
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if country:
        clauses.append("co.iso_code = ?")
        params.append(country.upper())
    if tier:
        clauses.append("sp.tier = ?")
        params.append(tier)
    if q:
        clauses.append("(lower(sp.name) LIKE ? OR lower(sp.supplier_code) LIKE ?)")
        params += [f"%{q.lower()}%", f"%{q.lower()}%"]
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"""SELECT sp.supplier_id, sp.supplier_code, sp.name, sp.tier, co.iso_code AS country
            FROM suppliers sp JOIN countries co USING (country_id) {where}
            ORDER BY sp.supplier_id LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    )
    return [dict(r) for r in rows]


@app.get("/suppliers/{supplier_id}")
def supplier_scorecard(supplier_id: int, conn: Any = Depends(get_conn)) -> dict[str, Any]:
    card = queries.supplier_scorecard(conn, supplier_id)
    if card is None:
        raise HTTPException(404, "supplier not found")
    return card


# ------------------------------------------------------------------ shipments
@app.get("/shipments", response_model=ShipmentPage)
def list_shipments(
    supplier_id: int | None = None,
    carrier_scac: str | None = None,
    status: Literal["in_transit", "delivered", "exception", "cancelled"] | None = None,
    ship_from: date | None = Query(None, description="Inclusive lower bound on ship_date"),
    ship_to: date | None = Query(None, description="Inclusive upper bound on ship_date"),
    late_only: bool = Query(False, description="Only delivered shipments that arrived after promised_date"),
    after_id: int | None = Query(None, description="Keyset cursor from previous page's next_after_id"),
    limit: int = Query(50, ge=1, le=500),
    conn: Any = Depends(get_conn),
) -> ShipmentPage:
    # Keyset pagination: cost is O(limit) regardless of how deep the client pages.
    clauses, params = [], []
    for cond, val in [("s.supplier_id = ?", supplier_id), ("c.scac_code = ?", carrier_scac and carrier_scac.upper()),
                      ("s.status = ?", status), ("s.ship_date >= ?", _iso(ship_from)),
                      ("s.ship_date <= ?", _iso(ship_to)), ("s.shipment_id > ?", after_id)]:
        if val is not None:
            clauses.append(cond)
            params.append(val)
    if late_only:
        clauses.append("s.delivered_date > s.promised_date")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(f"{shipment_select(conn)} {where} ORDER BY s.shipment_id LIMIT ?", [*params, limit + 1]).fetchall()
    items = [Shipment(**dict(r)) for r in rows[:limit]]
    return ShipmentPage(items=items, next_after_id=items[-1].shipment_id if len(rows) > limit else None)


@app.get("/shipments/lookup", response_model=Shipment)
def lookup_shipment(
    carrier_scac: str = Query(..., description="Tracking numbers are only unique per carrier, so both are required"),
    tracking_number: str = Query(...),
    conn: Any = Depends(get_conn),
) -> Shipment:
    row = conn.execute(f"{shipment_select(conn)} WHERE c.scac_code = ? AND s.tracking_number = ?",
                       (carrier_scac.upper(), tracking_number)).fetchone()
    if row is None:
        raise HTTPException(404, "shipment not found")
    return Shipment(**dict(row))


@app.get("/shipments/{shipment_id}", response_model=Shipment)
def get_shipment(shipment_id: int, conn: Any = Depends(get_conn)) -> Shipment:
    row = conn.execute(f"{shipment_select(conn)} WHERE s.shipment_id = ?", (shipment_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "shipment not found")
    return Shipment(**dict(row))


def _resolve(conn: Any, table: str, key_col: str, id_col: str, value: str, label: str) -> int:
    row = conn.execute(f"SELECT {id_col} FROM {table} WHERE {key_col} = ?", (value,)).fetchone()
    if row is None:
        raise HTTPException(422, f"unknown {label}: {value}")
    return row[0]


def _integrity_error(e: Exception) -> HTTPException:
    kind, name = violation(e)
    if kind == "unique" and name == "uq_shipments_carrier_tracking":
        return HTTPException(409, "a shipment with this tracking number already exists for this carrier")
    return HTTPException(422, f"constraint violation ({kind}{': ' + name if name else ''})")


@app.post("/shipments", response_model=Shipment, status_code=201)
def create_shipment(body: ShipmentCreate, conn: Any = Depends(get_conn)) -> Shipment:
    carrier_id = _resolve(conn, "carriers", "scac_code", "carrier_id", body.carrier_scac.upper(), "carrier")
    supplier_id = _resolve(conn, "suppliers", "supplier_code", "supplier_id", body.supplier_code, "supplier")
    wh_id = _resolve(conn, "warehouses", "code", "warehouse_id", body.warehouse_code, "warehouse")
    try:
        with transaction(conn):
            new_id = conn.execute(
                """INSERT INTO shipments (carrier_id, tracking_number, supplier_id, destination_warehouse_id,
                       ship_date, promised_date, delivered_date, status, weight_kg, freight_cost_cents)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING shipment_id""",
                (carrier_id, body.tracking_number, supplier_id, wh_id, _iso(body.ship_date),
                 _iso(body.promised_date), _iso(body.delivered_date), body.status, body.weight_kg,
                 body.freight_cost_cents),
            ).fetchone()[0]
    except IntegrityError as e:
        raise _integrity_error(e) from e
    if body.delivered_date:
        d = _iso(body.delivered_date)
        refresh_delay_metrics(conn, d, d)   # keep rollups exact for the touched day/month
    return get_shipment(new_id, conn)


@app.patch("/shipments/{shipment_id}", response_model=Shipment)
def update_shipment_status(shipment_id: int, body: ShipmentStatusUpdate,
                           conn: Any = Depends(get_conn)) -> Shipment:
    old = conn.execute("SELECT delivered_date FROM shipments WHERE shipment_id = ?", (shipment_id,)).fetchone()
    if old is None:
        raise HTTPException(404, "shipment not found")
    try:
        with transaction(conn):
            conn.execute("UPDATE shipments SET status = ?, delivered_date = ? WHERE shipment_id = ?",
                         (body.status, _iso(body.delivered_date), shipment_id))
    except IntegrityError as e:
        raise _integrity_error(e) from e
    for d in {old["delivered_date"], _iso(body.delivered_date)} - {None}:
        refresh_delay_metrics(conn, d, d)
    return get_shipment(shipment_id, conn)


# ------------------------------------------------------------------ analytics
@app.get("/analytics/summary")
def analytics_summary(
    date_from: date | None = FROM_Q, date_to: date | None = TO_Q,
    as_of: date | None = Query(None, description="Reference date for overdue in-transit count (default today)"),
    source: Source = SOURCE_Q, conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    _check_range(date_from, date_to)
    return queries.summary(conn, _iso(date_from), _iso(date_to), _iso(as_of or date.today()), source)


@app.get("/analytics/delays/by-carrier")
def analytics_by_carrier(
    date_from: date | None = FROM_Q, date_to: date | None = TO_Q,
    source: Source = SOURCE_Q, conn: Any = Depends(get_conn),
) -> list[dict[str, Any]]:
    _check_range(date_from, date_to)
    return queries.delays_by_carrier(conn, _iso(date_from), _iso(date_to), source)


@app.get("/analytics/delays/by-supplier")
def analytics_by_supplier(
    date_from: date | None = FROM_Q, date_to: date | None = TO_Q,
    source: Source = SOURCE_Q,
    order: Literal["worst", "best"] = "worst",
    min_delivered: int = Query(20, ge=1, description="Exclude suppliers with too few deliveries to rank fairly"),
    limit: int = Query(25, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: Any = Depends(get_conn),
) -> list[dict[str, Any]]:
    _check_range(date_from, date_to)
    try:
        return queries.delays_by_supplier(conn, _iso(date_from), _iso(date_to), source,
                                          min_delivered, order, limit, offset)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/analytics/delays/trend")
def analytics_trend(
    date_from: date | None = FROM_Q, date_to: date | None = TO_Q,
    carrier_id: int | None = None, source: Source = SOURCE_Q,
    conn: Any = Depends(get_conn),
) -> list[dict[str, Any]]:
    _check_range(date_from, date_to)
    return queries.delay_trend(conn, _iso(date_from), _iso(date_to), carrier_id, source)


@app.post("/admin/refresh-metrics")
def admin_refresh(date_from: date | None = FROM_Q, date_to: date | None = TO_Q,
                  conn: Any = Depends(get_conn)) -> dict[str, Any]:
    _check_range(date_from, date_to)
    return {"rows_written": refresh_delay_metrics(conn, _iso(date_from), _iso(date_to))}
