"""Integrity is enforced by the database itself, not just the API layer."""
import sqlite3

import pytest

INSERT = """INSERT INTO shipments (carrier_id, tracking_number, supplier_id, destination_warehouse_id,
              ship_date, promised_date, delivered_date, status, weight_kg, freight_cost_cents)
            VALUES (?, ?, 1, 1, ?, ?, ?, ?, 10.0, 1000)"""


def ins(conn, carrier=1, tn="TEST000001", ship="2025-01-01", promised="2025-01-05",
        delivered=None, status="in_transit"):
    conn.execute(INSERT, (carrier, tn, ship, promised, delivered, status))


def test_same_tracking_number_same_carrier_rejected(conn):
    ins(conn, carrier=1, tn="DUPLICATE01")
    with pytest.raises(sqlite3.IntegrityError, match="shipments.carrier_id, shipments.tracking_number"):
        ins(conn, carrier=1, tn="DUPLICATE01")


def test_same_tracking_number_different_carrier_allowed(conn):
    ins(conn, carrier=1, tn="SHARED00001")
    ins(conn, carrier=2, tn="SHARED00001")
    n = conn.execute("SELECT COUNT(*) FROM shipments WHERE tracking_number = 'SHARED00001'").fetchone()[0]
    assert n == 2


def test_seed_contains_cross_carrier_collisions(conn):
    shared = conn.execute("""SELECT COUNT(*) FROM (SELECT tracking_number FROM shipments
                             GROUP BY tracking_number HAVING COUNT(DISTINCT carrier_id) > 1)""").fetchone()[0]
    dup_within = conn.execute("""SELECT COUNT(*) FROM (SELECT 1 FROM shipments
                                 GROUP BY carrier_id, tracking_number HAVING COUNT(*) > 1)""").fetchone()[0]
    assert shared > 0 and dup_within == 0


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        ins(conn, carrier=9999)


@pytest.mark.parametrize("kwargs", [
    dict(ship="2025-01-10", promised="2025-01-05"),                               # promised before ship
    dict(delivered="2024-12-31", status="delivered"),                             # delivered before ship
    dict(delivered=None, status="delivered"),                                     # delivered w/o date
    dict(delivered="2025-01-04", status="in_transit"),                            # date w/o delivered
    dict(status="lost"),                                                          # bad enum
    dict(ship="2025-02-30"),                                                      # impossible date
    dict(ship="01/01/2025"),                                                      # wrong format
])
def test_check_constraints(conn, kwargs):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        ins(conn, **kwargs)


def test_natural_keys_unique(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO carriers (scac_code, name, mode) VALUES ('NRFX', 'dup', 'road')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO suppliers (supplier_code, name, country_id, tier, onboarded_on) "
                     "VALUES ('SUP-00001', 'dup', 1, 1, '2020-01-01')")


def test_lookup_uses_composite_index(conn):
    plan = " ".join(r[-1] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM shipments WHERE carrier_id = 1 AND tracking_number = 'X'"))
    assert "sqlite_autoindex_shipments_1" in plan or "uq_shipments" in plan
