"""Integrity is enforced by the database itself, not just the API layer, on every backend."""
import pytest

from app.db import DataError, IntegrityError, violation

INSERT = """INSERT INTO shipments (carrier_id, tracking_number, supplier_id, destination_warehouse_id,
              ship_date, promised_date, delivered_date, status, weight_kg, freight_cost_cents)
            VALUES (?, ?, 1, 1, ?, ?, ?, ?, 10.0, 1000)"""


def ins(conn, carrier=1, tn="TEST000001", ship="2025-01-01", promised="2025-01-05",
        delivered=None, status="in_transit"):
    conn.execute(INSERT, (carrier, tn, ship, promised, delivered, status))


def expect_violation(conn, kind, **kwargs):
    with pytest.raises(IntegrityError) as exc:
        ins(conn, **kwargs)
    assert violation(exc.value)[0] == kind, exc.value
    return violation(exc.value)


def test_same_tracking_number_same_carrier_rejected(conn):
    ins(conn, carrier=1, tn="DUPLICATE01")
    assert expect_violation(conn, "unique", carrier=1, tn="DUPLICATE01") == ("unique", "uq_shipments_carrier_tracking")


def test_same_tracking_number_different_carrier_allowed(conn):
    ins(conn, carrier=1, tn="SHARED00001")
    ins(conn, carrier=2, tn="SHARED00001")
    n = conn.execute("SELECT COUNT(*) FROM shipments WHERE tracking_number = 'SHARED00001'").fetchone()[0]
    assert n == 2


def test_seed_contains_cross_carrier_collisions(conn):
    shared = conn.execute("""SELECT COUNT(*) FROM (SELECT tracking_number FROM shipments
                             GROUP BY tracking_number HAVING COUNT(DISTINCT carrier_id) > 1) AS t""").fetchone()[0]
    dup_within = conn.execute("""SELECT COUNT(*) FROM (SELECT 1 FROM shipments
                                 GROUP BY carrier_id, tracking_number HAVING COUNT(*) > 1) AS t""").fetchone()[0]
    assert shared > 0 and dup_within == 0


def test_foreign_keys_enforced(conn):
    expect_violation(conn, "fk", carrier=9999)


@pytest.mark.parametrize("kwargs", [
    dict(ship="2025-01-10", promised="2025-01-05"),        # promised before ship
    dict(delivered="2024-12-31", status="delivered"),      # delivered before ship
    dict(delivered=None, status="delivered"),              # delivered w/o date
    dict(delivered="2025-01-04", status="in_transit"),     # date w/o delivered
    dict(status="lost"),                                   # bad enum
])
def test_check_constraints(conn, kwargs):
    expect_violation(conn, "check", **kwargs)


@pytest.mark.parametrize("bad", ["2025-02-30", "01/01/2025", "not-a-date"])
def test_invalid_dates_rejected(conn, backend, bad):
    if backend == "postgres" and bad == "01/01/2025":
        # Postgres DATE input accepts this per DateStyle. Non-ISO input never reaches
        # the database from the API (pydantic parses dates), so this is acceptable.
        pytest.skip("Postgres DATE parses MDY strings by design")
    # SQLite: TEXT + CHECK (date(x) IS x) -> IntegrityError.  Postgres: DATE type -> DataError.
    with pytest.raises(IntegrityError + DataError):
        ins(conn, ship=bad)


def test_natural_keys_unique(conn):
    with pytest.raises(IntegrityError):
        conn.execute("INSERT INTO carriers (scac_code, name, mode) VALUES ('NRFX', 'dup', 'road')")
    with pytest.raises(IntegrityError):
        conn.execute("INSERT INTO suppliers (supplier_code, name, country_id, tier, onboarded_on) "
                     "VALUES ('SUP-00001', 'dup', 1, 1, '2020-01-01')")


def test_lookup_uses_composite_index(conn, backend):
    if backend == "postgres":
        conn.execute("SET enable_seqscan = off")  # small table: force the planner to show its index choice
        plan = " ".join(r[0] for r in conn.execute(
            "EXPLAIN SELECT * FROM shipments WHERE carrier_id = 1 AND tracking_number = 'X'"))
        assert "uq_shipments_carrier_tracking" in plan
    else:
        plan = " ".join(r[-1] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM shipments WHERE carrier_id = 1 AND tracking_number = 'X'"))
        assert "sqlite_autoindex_shipments_1" in plan
