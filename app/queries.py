"""Analytics queries.

Every delay metric can be served from the live `shipments` table or from the
precomputed rollups. Both paths return identical numbers (tests assert this);
the rollup path is what keeps response times flat as shipment volume grows.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from .db import Dialect
from .metrics import delay_expr

Source = Literal["rollup", "live"]

# Shared SELECT list for metric aggregates over the *rollup* tables.
_ROLLUP_AGG = """
    COALESCE(SUM(r.delivered_count), 0)  AS delivered,
    COALESCE(SUM(r.late_count), 0)       AS late,
    COALESCE(SUM(r.total_delay_days), 0) AS total_delay_days,
    COALESCE(MAX(r.max_delay_days), 0)   AS max_delay_days
"""


def _live_agg(d: Dialect) -> str:
    """The same aggregates computed from raw shipments."""
    delay = delay_expr(d)
    return f"""
    COUNT(*)                                                    AS delivered,
    COALESCE(SUM(CASE WHEN {delay} > 0 THEN 1 ELSE 0 END), 0)   AS late,
    COALESCE(SUM({d.pos(delay)}), 0)                            AS total_delay_days,
    COALESCE(MAX({d.pos(delay)}), 0)                            AS max_delay_days
"""


def _metrics(row) -> dict[str, Any]:
    d = dict(row)
    delivered, late, total = d["delivered"], d["late"], d["total_delay_days"]
    d["on_time_rate"] = round(1 - late / delivered, 4) if delivered else None
    d["avg_delay_days"] = round(total / delivered, 3) if delivered else None
    d["avg_days_late_when_late"] = round(total / late, 3) if late else None
    return d


def _range(column: str, date_from: str | None, date_to: str | None) -> tuple[list[str], list[Any]]:
    clauses, params = [], []
    if date_from:
        clauses.append(f"{column} >= ?")
        params.append(date_from)
    if date_to:
        clauses.append(f"{column} <= ?")
        params.append(date_to)
    return clauses, params


def _where(clauses: list[str]) -> str:
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


def _month_range(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    """Supplier rollup is monthly; only whole-month windows can be answered exactly."""
    if date_from and not date_from.endswith("-01"):
        raise ValueError("source=rollup for supplier metrics needs date_from on the 1st of a month; use source=live")
    if date_to:
        if (date.fromisoformat(date_to) + timedelta(days=1)).day != 1:
            raise ValueError("source=rollup for supplier metrics needs date_to on the last day of a month; use source=live")
    return (date_from[:7] if date_from else None, date_to[:7] if date_to else None)


# --------------------------------------------------------------------------- summary
def summary(conn, date_from: str | None, date_to: str | None,
            as_of: str, source: Source) -> dict[str, Any]:
    if source == "rollup":
        clauses, params = _range("r.metric_date", date_from, date_to)
        row = conn.execute(
            f"SELECT {_ROLLUP_AGG} FROM delay_metrics_carrier_daily r {_where(clauses)}", params
        ).fetchone()
    else:
        clauses, params = _range("s.delivered_date", date_from, date_to)
        row = conn.execute(
            f"SELECT {_live_agg(conn.dialect)} FROM shipments s {_where(['s.delivered_date IS NOT NULL', *clauses])}",
            params,
        ).fetchone()
    out = _metrics(row)

    # Open-pipeline snapshot is always live: it is small (indexed on status) and must be current.
    open_row = conn.execute(
        """SELECT
             SUM(CASE WHEN status = 'in_transit' THEN 1 ELSE 0 END)                         AS in_transit,
             SUM(CASE WHEN status = 'in_transit' AND promised_date < ? THEN 1 ELSE 0 END)   AS overdue_in_transit,
             SUM(CASE WHEN status = 'exception' THEN 1 ELSE 0 END)                          AS exceptions
           FROM shipments WHERE status IN ('in_transit', 'exception')""",
        (as_of,),
    ).fetchone()
    out.update({k: open_row[k] or 0 for k in open_row.keys()})
    out.update(date_from=date_from, date_to=date_to, as_of=as_of, source=source)
    return out


# --------------------------------------------------------------------------- by carrier
def delays_by_carrier(conn, date_from: str | None, date_to: str | None,
                      source: Source) -> list[dict[str, Any]]:
    if source == "rollup":
        clauses, params = _range("r.metric_date", date_from, date_to)
        sql = f"""SELECT c.carrier_id, c.scac_code, c.name, c.mode, {_ROLLUP_AGG}
                  FROM delay_metrics_carrier_daily r JOIN carriers c USING (carrier_id)
                  {_where(clauses)} GROUP BY c.carrier_id ORDER BY c.carrier_id"""
    else:
        clauses, params = _range("s.delivered_date", date_from, date_to)
        sql = f"""SELECT c.carrier_id, c.scac_code, c.name, c.mode, {_live_agg(conn.dialect)}
                  FROM shipments s JOIN carriers c USING (carrier_id)
                  {_where(['s.delivered_date IS NOT NULL', *clauses])} GROUP BY c.carrier_id ORDER BY c.carrier_id"""
    rows = [_metrics(r) for r in conn.execute(sql, params)]
    return sorted(rows, key=lambda r: (r["on_time_rate"] is None, r["on_time_rate"] or 0))


# --------------------------------------------------------------------------- by supplier
def delays_by_supplier(conn, date_from: str | None, date_to: str | None,
                       source: Source, min_delivered: int, order: Literal["worst", "best"],
                       limit: int, offset: int) -> list[dict[str, Any]]:
    if source == "rollup":
        m_from, m_to = _month_range(date_from, date_to)
        clauses, params = _range("r.metric_month", m_from, m_to)
        base = f"""SELECT sp.supplier_id, sp.supplier_code, sp.name, co.iso_code AS country, sp.tier, {_ROLLUP_AGG}
                   FROM delay_metrics_supplier_monthly r
                   JOIN suppliers sp USING (supplier_id) JOIN countries co USING (country_id)
                   {_where(clauses)} GROUP BY sp.supplier_id, co.country_id"""
    else:
        clauses, params = _range("s.delivered_date", date_from, date_to)
        base = f"""SELECT sp.supplier_id, sp.supplier_code, sp.name, co.iso_code AS country, sp.tier, {_live_agg(conn.dialect)}
                   FROM shipments s
                   JOIN suppliers sp USING (supplier_id) JOIN countries co USING (country_id)
                   {_where(['s.delivered_date IS NOT NULL', *clauses])} GROUP BY sp.supplier_id, co.country_id"""
    direction = "DESC" if order == "worst" else "ASC"
    sql = f"""SELECT * FROM ({base}) AS t WHERE delivered >= ?
              ORDER BY CAST(late AS DOUBLE PRECISION) / delivered {direction}, total_delay_days {direction}, supplier_id
              LIMIT ? OFFSET ?"""
    return [_metrics(r) for r in conn.execute(sql, [*params, min_delivered, limit, offset])]


# --------------------------------------------------------------------------- trend
def delay_trend(conn, date_from: str | None, date_to: str | None,
                carrier_id: int | None, source: Source) -> list[dict[str, Any]]:
    if source == "rollup":
        clauses, params = _range("r.metric_date", date_from, date_to)
        if carrier_id is not None:
            clauses.append("r.carrier_id = ?")
            params.append(carrier_id)
        sql = f"""SELECT {conn.dialect.month('r.metric_date')} AS month, {_ROLLUP_AGG}
                  FROM delay_metrics_carrier_daily r {_where(clauses)} GROUP BY month ORDER BY month"""
    else:
        clauses, params = _range("s.delivered_date", date_from, date_to)
        if carrier_id is not None:
            clauses.append("s.carrier_id = ?")
            params.append(carrier_id)
        sql = f"""SELECT {conn.dialect.month('s.delivered_date')} AS month, {_live_agg(conn.dialect)}
                  FROM shipments s {_where(['s.delivered_date IS NOT NULL', *clauses])}
                  GROUP BY month ORDER BY month"""
    return [_metrics(r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------------------- supplier scorecard
def supplier_scorecard(conn, supplier_id: int) -> dict[str, Any] | None:
    sup = conn.execute(
        """SELECT sp.supplier_id, sp.supplier_code, sp.name, sp.tier, sp.onboarded_on,
                  co.iso_code AS country, co.name AS country_name, rg.name AS region
           FROM suppliers sp JOIN countries co USING (country_id) JOIN regions rg USING (region_id)
           WHERE sp.supplier_id = ?""",
        (supplier_id,),
    ).fetchone()
    if sup is None:
        return None
    totals = conn.execute(
        f"SELECT {_ROLLUP_AGG} FROM delay_metrics_supplier_monthly r WHERE r.supplier_id = ?", (supplier_id,)
    ).fetchone()
    monthly = conn.execute(
        f"""SELECT r.metric_month AS month, {_ROLLUP_AGG} FROM delay_metrics_supplier_monthly r
            WHERE r.supplier_id = ? GROUP BY r.metric_month ORDER BY r.metric_month""",
        (supplier_id,),
    ).fetchall()
    by_status = conn.execute(
        "SELECT status, COUNT(*) AS n FROM shipments WHERE supplier_id = ? GROUP BY status", (supplier_id,)
    ).fetchall()
    return {
        **dict(sup),
        "totals": _metrics(totals),
        "shipments_by_status": {r["status"]: r["n"] for r in by_status},
        "monthly": [_metrics(r) for r in monthly],
    }
