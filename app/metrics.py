"""Precomputed delivery-delay metrics.

Two rollups, both keyed by delivered_date and holding only additive counters:

  delay_metrics_carrier_daily     (metric_date,  carrier_id)   -> summary, trend, by-carrier
  delay_metrics_supplier_monthly  (metric_month, supplier_id)  -> supplier rankings / scorecards

Refresh is idempotent: the affected window is deleted and rebuilt in a single
transaction, so it can be re-run for late-arriving or corrected shipments.
The supplier rollup is monthly, so its window is widened to whole months.

Delay definition (shared by live queries and the rollups):
    delay_days = julianday(delivered_date) - julianday(promised_date)
    late       = delay_days > 0
Only delivered shipments have a delay.
"""
from __future__ import annotations

import sqlite3
from datetime import date

from .db import transaction

DELAY_EXPR = "CAST(julianday(s.delivered_date) - julianday(s.promised_date) AS INTEGER)"

AGGREGATES = f"""
    COUNT(*),
    SUM(CASE WHEN {DELAY_EXPR} > 0 THEN 1 ELSE 0 END),
    SUM(MAX({DELAY_EXPR}, 0)),
    MAX(MAX({DELAY_EXPR}, 0))
"""


def _month_bounds(from_date: str | None, to_date: str | None) -> tuple[str | None, str | None]:
    """Widen [from, to] to whole months: ('2025-03-14','2025-05-02') -> ('2025-03-01','2025-05-31')."""
    lo = from_date[:7] + "-01" if from_date else None
    hi = None
    if to_date:
        d = date.fromisoformat(to_date)
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        hi = date.fromordinal(nxt.toordinal() - 1).isoformat()
    return lo, hi


def _window(column: str, lo: str | None, hi: str | None) -> tuple[str, list[str]]:
    clauses, params = [], []
    if lo:
        clauses.append(f"{column} >= ?")
        params.append(lo)
    if hi:
        clauses.append(f"{column} <= ?")
        params.append(hi)
    return (" AND ".join(clauses) or "1=1"), params


def refresh_delay_metrics(
    conn: sqlite3.Connection, from_date: str | None = None, to_date: str | None = None
) -> int:
    """Rebuild rollups for delivered_date in [from_date, to_date] (inclusive; None = unbounded).

    Returns the total number of rollup rows written.
    """
    m_lo, m_hi = _month_bounds(from_date, to_date)

    with transaction(conn):
        # carrier x day
        w, p = _window("metric_date", from_date, to_date)
        conn.execute(f"DELETE FROM delay_metrics_carrier_daily WHERE {w}", p)
        w, p = _window("s.delivered_date", from_date, to_date)
        written = conn.execute(
            f"""INSERT INTO delay_metrics_carrier_daily
                    (metric_date, carrier_id, delivered_count, late_count, total_delay_days, max_delay_days)
                SELECT s.delivered_date, s.carrier_id, {AGGREGATES}
                FROM shipments s
                WHERE s.delivered_date IS NOT NULL AND {w}
                GROUP BY s.delivered_date, s.carrier_id""",
            p,
        ).rowcount

        # supplier x month (window widened to whole months)
        w, p = _window("metric_month", m_lo and m_lo[:7], m_hi and m_hi[:7])
        conn.execute(f"DELETE FROM delay_metrics_supplier_monthly WHERE {w}", p)
        w, p = _window("s.delivered_date", m_lo, m_hi)
        written += conn.execute(
            f"""INSERT INTO delay_metrics_supplier_monthly
                    (metric_month, supplier_id, delivered_count, late_count, total_delay_days, max_delay_days)
                SELECT substr(s.delivered_date, 1, 7), s.supplier_id, {AGGREGATES}
                FROM shipments s
                WHERE s.delivered_date IS NOT NULL AND {w}
                GROUP BY substr(s.delivered_date, 1, 7), s.supplier_id""",
            p,
        ).rowcount

        conn.execute(
            "INSERT INTO metrics_refresh_log (from_date, to_date, rows_written) VALUES (?, ?, ?)",
            (from_date, to_date, written),
        )
    return written
