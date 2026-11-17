"""The precomputed rollups must agree exactly with live computation."""
import pytest

from app import queries
from app.metrics import refresh_delay_metrics

KEYS = ("delivered", "late", "total_delay_days", "max_delay_days", "on_time_rate")
RANGES = [(None, None), ("2025-01-01", "2025-06-30"), ("2025-03-14", "2025-03-20"), ("2026-02-01", None)]


def strip(rows, key):
    return {r[key]: {k: r[k] for k in KEYS} for r in rows}


@pytest.mark.parametrize("lo,hi", RANGES)
def test_summary_parity(conn, lo, hi):
    a = queries.summary(conn, lo, hi, "2026-11-15", "rollup")
    b = queries.summary(conn, lo, hi, "2026-11-15", "live")
    assert {k: a[k] for k in KEYS} == {k: b[k] for k in KEYS}
    assert a["delivered"] > 0


@pytest.mark.parametrize("lo,hi", RANGES)
def test_carrier_and_trend_parity(conn, lo, hi):
    assert strip(queries.delays_by_carrier(conn, lo, hi, "rollup"), "carrier_id") == \
           strip(queries.delays_by_carrier(conn, lo, hi, "live"), "carrier_id")
    assert strip(queries.delay_trend(conn, lo, hi, None, "rollup"), "month") == \
           strip(queries.delay_trend(conn, lo, hi, None, "live"), "month")


@pytest.mark.parametrize("lo,hi", [(None, None), ("2025-01-01", "2025-06-30"), ("2024-02-01", "2024-02-29")])
def test_supplier_parity_month_aligned(conn, lo, hi):
    args = (lo, hi)
    rollup = queries.delays_by_supplier(conn, *args, "rollup", 1, "worst", 10_000, 0)
    live = queries.delays_by_supplier(conn, *args, "live", 1, "worst", 10_000, 0)
    assert strip(rollup, "supplier_id") == strip(live, "supplier_id")
    assert [r["supplier_id"] for r in rollup] == [r["supplier_id"] for r in live]


def test_supplier_rollup_rejects_partial_months(conn):
    with pytest.raises(ValueError):
        queries.delays_by_supplier(conn, "2025-01-15", None, "rollup", 1, "worst", 10, 0)
    with pytest.raises(ValueError):
        queries.delays_by_supplier(conn, None, "2025-01-30", "rollup", 1, "worst", 10, 0)


def test_incremental_refresh_matches_full_rebuild(conn):
    # Corrupt a window of the rollups, then repair just that window.
    conn.execute("UPDATE delay_metrics_carrier_daily SET late_count = 0 "
                 "WHERE metric_date BETWEEN '2025-04-01' AND '2025-04-30'")
    conn.execute("UPDATE delay_metrics_supplier_monthly SET late_count = 0 WHERE metric_month = '2025-04'")
    refresh_delay_metrics(conn, "2025-04-10", "2025-04-30")   # partial month: supplier window widens
    after_incremental = (
        conn.execute("SELECT * FROM delay_metrics_carrier_daily WHERE metric_date >= '2025-04-10' ORDER BY 1, 2").fetchall(),
        conn.execute("SELECT * FROM delay_metrics_supplier_monthly ORDER BY 1, 2").fetchall(),
    )
    refresh_delay_metrics(conn)
    full = (
        conn.execute("SELECT * FROM delay_metrics_carrier_daily WHERE metric_date >= '2025-04-10' ORDER BY 1, 2").fetchall(),
        conn.execute("SELECT * FROM delay_metrics_supplier_monthly ORDER BY 1, 2").fetchall(),
    )
    assert [tuple(r) for r in after_incremental[0]] == [tuple(r) for r in full[0]]
    assert [tuple(r) for r in after_incremental[1]] == [tuple(r) for r in full[1]]


def test_refresh_is_idempotent(conn):
    before = conn.execute("SELECT COUNT(*), SUM(late_count) FROM delay_metrics_carrier_daily").fetchone()
    refresh_delay_metrics(conn)
    refresh_delay_metrics(conn)
    assert tuple(conn.execute("SELECT COUNT(*), SUM(late_count) FROM delay_metrics_carrier_daily").fetchone()) == tuple(before)


def test_rollup_is_much_smaller_than_fact_table(conn):
    facts = conn.execute("SELECT COUNT(*) FROM shipments WHERE delivered_date IS NOT NULL").fetchone()[0]
    daily = conn.execute("SELECT COUNT(*) FROM delay_metrics_carrier_daily").fetchone()[0]
    assert daily < facts
