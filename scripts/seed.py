"""Generate a reproducible synthetic logistics dataset.

    python -m scripts.seed                      # 1,200 suppliers, 150,000 shipments
    python -m scripts.seed --shipments 500000   # bigger run
    python -m scripts.seed --db postgresql://user:pass@host/dbname   # same data into Postgres

Notes on realism
  * Each supplier and carrier gets a latent reliability score; delays are drawn
    from both, so rankings in the dashboard are meaningful rather than noise.
  * ~2% of tracking numbers deliberately reuse a string already issued by a
    *different* carrier. This is legal (the composite UNIQUE allows it) and
    exercises the exact collision case the constraint exists for.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect, default_target, init_schema, reset_database, sync_identity_sequences, transaction  # noqa: E402
from app.metrics import refresh_delay_metrics  # noqa: E402

AS_OF = date(2026, 11, 15)          # "today" for the dataset; later deliveries are still in transit
START = date(2024, 1, 1)
END = date(2026, 11, 15)

REGIONS = {
    "North America": [("US", "United States"), ("CA", "Canada"), ("MX", "Mexico")],
    "Europe": [("DE", "Germany"), ("NL", "Netherlands"), ("PL", "Poland"), ("IT", "Italy"),
               ("ES", "Spain"), ("FR", "France"), ("GB", "United Kingdom")],
    "Asia Pacific": [("CN", "China"), ("VN", "Vietnam"), ("IN", "India"), ("JP", "Japan"),
                     ("KR", "South Korea"), ("TH", "Thailand"), ("ID", "Indonesia"), ("AU", "Australia")],
    "Latin America": [("BR", "Brazil"), ("CL", "Chile"), ("CO", "Colombia")],
    "Middle East & Africa": [("TR", "Turkey"), ("AE", "United Arab Emirates"), ("ZA", "South Africa"),
                             ("MA", "Morocco")],
}

# Fictional carriers. (scac, name, mode, tracking format)
CARRIERS = [
    ("NRFX", "Northfreight Express", "road", "num10"),
    ("BLRL", "Blue Line Rail", "rail", "num10"),
    ("SWAY", "Sternway Logistics", "road", "num10"),
    ("KPAR", "Kestrel Parcel", "air", "alnum12"),
    ("HALO", "Halo Air Cargo", "air", "alnum12"),
    ("OCMR", "Oceanmere Lines", "ocean", "container"),
    ("TDWT", "Tidewater Shipping", "ocean", "container"),
    ("PNCL", "Pinnacle Freight", "road", "num10"),
    ("RVTR", "Rivertrail Haulage", "road", "num10"),
    ("CRSN", "Crestline Rail", "rail", "num10"),
    ("ZPHR", "Zephyr Air", "air", "alnum12"),
    ("MRDN", "Meridian Ocean", "ocean", "container"),
    ("GRNT", "Granite Transport", "road", "num10"),
    ("SLVR", "Silverleaf Couriers", "air", "alnum12"),
    ("ATLS", "Atlas Intermodal", "rail", "num10"),
    ("CBLT", "Cobalt Carriers", "road", "num10"),
    ("HRBR", "Harborpoint Marine", "ocean", "container"),
    ("VLCT", "Velocity Linehaul", "road", "num10"),
]

TRANSIT_DAYS = {"air": (2, 5), "road": (3, 10), "rail": (7, 16), "ocean": (21, 45)}
MODE_WEIGHTS = {"air": 0.15, "road": 0.50, "rail": 0.15, "ocean": 0.20}

WAREHOUSES = [
    ("DC-ATL", "Atlanta DC", "US"), ("DC-DFW", "Dallas DC", "US"), ("DC-LAX", "Los Angeles DC", "US"),
    ("DC-CHI", "Chicago DC", "US"), ("DC-TOR", "Toronto DC", "CA"), ("DC-MEX", "Mexico City DC", "MX"),
    ("DC-RTM", "Rotterdam DC", "NL"), ("DC-FRA", "Frankfurt DC", "DE"), ("DC-WAW", "Warsaw DC", "PL"),
    ("DC-SIN", "Singapore DC", "TH"), ("DC-SYD", "Sydney DC", "AU"), ("DC-SAO", "Sao Paulo DC", "BR"),
]

NAME_A = ["Apex", "Summit", "Harbor", "Vertex", "Pioneer", "Evergreen", "Ironwood", "Lumen", "Nova",
          "Orchid", "Quantum", "Redwood", "Sable", "Titan", "Union", "Vista", "Willow", "Zenith",
          "Aurora", "Beacon", "Cedar", "Delta", "Ember", "Falcon", "Golden", "Horizon"]
NAME_B = ["Components", "Industries", "Manufacturing", "Textiles", "Plastics", "Metals", "Electronics",
          "Packaging", "Foods", "Chemicals", "Polymers", "Fabrication", "Supply", "Materials"]
NAME_C = ["Co.", "Ltd.", "GmbH", "S.A.", "Inc.", "LLC", "Pvt. Ltd.", "B.V.", "Sp. z o.o."]


def tracking_number(rng: random.Random, fmt: str) -> str:
    if fmt == "num10":
        return str(rng.randrange(10**9, 10**10))
    if fmt == "alnum12":
        return "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(12))
    # ISO 6346-ish container id: 4 letters + 7 digits
    return "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(3)) + "U" + str(rng.randrange(10**6, 10**7))


def seed(target: str | Path, n_suppliers: int, n_shipments: int, rng_seed: int = 42) -> None:
    """Build the dataset into `target` (SQLite path or Postgres URL). Same seed -> identical rows on both."""
    rng = random.Random(rng_seed)
    reset_database(target)
    conn = connect(target)
    init_schema(conn)
    t0 = time.perf_counter()

    with transaction(conn):
        country_ids: dict[str, int] = {}
        for region_id, (region, countries) in enumerate(REGIONS.items(), start=1):
            conn.execute("INSERT INTO regions (region_id, name) VALUES (?, ?)", (region_id, region))
            for iso, name in countries:
                country_ids[iso] = conn.execute(
                    "INSERT INTO countries (iso_code, name, region_id) VALUES (?, ?, ?) RETURNING country_id",
                    (iso, name, region_id),
                ).fetchone()[0]

        carriers = []  # (carrier_id, mode, fmt, reliability)
        for scac, name, mode, fmt in CARRIERS:
            carrier_id = conn.execute("INSERT INTO carriers (scac_code, name, mode) VALUES (?, ?, ?) RETURNING carrier_id",
                                      (scac, name, mode)).fetchone()[0]
            carriers.append((carrier_id, mode, fmt, rng.betavariate(8, 2)))

        warehouse_ids = []
        for code, name, iso in WAREHOUSES:
            warehouse_ids.append(conn.execute(
                "INSERT INTO warehouses (code, name, country_id) VALUES (?, ?, ?) RETURNING warehouse_id",
                (code, name, country_ids[iso]),
            ).fetchone()[0])

        # Supplier mix skews toward manufacturing hubs.
        iso_pool = list(country_ids)
        iso_weights = [6 if iso in ("CN", "VN", "IN", "MX", "DE", "PL", "US") else 1 for iso in iso_pool]
        supplier_rows, supplier_meta = [], []
        for i in range(1, n_suppliers + 1):
            iso = rng.choices(iso_pool, iso_weights)[0]
            name = f"{rng.choice(NAME_A)} {rng.choice(NAME_B)} {rng.choice(NAME_C)}"
            tier = rng.choices([1, 2, 3], [0.2, 0.5, 0.3])[0]
            onboarded = START - timedelta(days=rng.randrange(0, 2000))
            supplier_rows.append((i, f"SUP-{i:05d}", name, country_ids[iso], tier, onboarded.isoformat()))
            # reliability in (0,1]; tier-3 suppliers are a bit worse on average
            supplier_meta.append((i, rng.betavariate(6 - tier, 1.5), rng.paretovariate(1.2)))
        conn.bulk_insert("suppliers", ["supplier_id", "supplier_code", "name", "country_id", "tier", "onboarded_on"],
                         supplier_rows)

        by_mode: dict[str, list] = {}
        for c in carriers:
            by_mode.setdefault(c[1], []).append(c)
        modes, mode_w = list(MODE_WEIGHTS), list(MODE_WEIGHTS.values())
        sup_weights = [m[2] for m in supplier_meta]  # heavy-tailed volume per supplier
        span_days = (END - START).days

        issued: dict[int, set[str]] = {c[0]: set() for c in carriers}
        all_issued: list[tuple[int, str]] = []
        rows = []
        for _ in range(n_shipments):
            sup_id, sup_rel, _ = rng.choices(supplier_meta, sup_weights)[0]
            mode = rng.choices(modes, mode_w)[0]
            carrier_id, _, fmt, car_rel = rng.choice(by_mode[mode])

            # Intentional cross-carrier collision: reuse another carrier's number.
            tn = None
            if all_issued and rng.random() < 0.02:
                other_carrier, other_tn = rng.choice(all_issued)
                if other_carrier != carrier_id and other_tn not in issued[carrier_id]:
                    tn = other_tn
            while tn is None or tn in issued[carrier_id]:
                tn = tracking_number(rng, fmt)
            issued[carrier_id].add(tn)
            all_issued.append((carrier_id, tn))

            ship = START + timedelta(days=rng.randrange(span_days))
            lo, hi = TRANSIT_DAYS[mode]
            promised = ship + timedelta(days=rng.randint(lo, hi))

            # Delay model: reliability drives the chance and size of lateness.
            reliability = (sup_rel * car_rel) ** 0.5
            if rng.random() < reliability:
                delay = -rng.choice([0, 0, 0, 1, 1, 2])          # on time or early
            else:
                scale = (hi - lo) * 0.4 + 1
                delay = 1 + int(rng.expovariate(1 / scale))
            delivered = max(promised + timedelta(days=delay), ship)

            r = rng.random()
            if r < 0.01:
                status, delivered_s = "cancelled", None
            elif r < 0.025:
                status, delivered_s = "exception", None
            elif delivered > AS_OF:
                status, delivered_s = "in_transit", None
            else:
                status, delivered_s = "delivered", delivered.isoformat()

            weight = round(rng.lognormvariate(5.5, 1.1), 2)
            rate = {"air": 450, "road": 35, "rail": 20, "ocean": 12}[mode]  # cents per kg
            cost = int(weight * rate * rng.uniform(0.8, 1.3)) + 2500

            rows.append((carrier_id, tn, sup_id, rng.choice(warehouse_ids), ship.isoformat(),
                         promised.isoformat(), delivered_s, status, weight, cost))

        # Explicit shipment_ids keep both engines byte-for-byte comparable.
        conn.bulk_insert("shipments", ["shipment_id", "carrier_id", "tracking_number", "supplier_id",
                                       "destination_warehouse_id", "ship_date", "promised_date", "delivered_date",
                                       "status", "weight_kg", "freight_cost_cents"],
                         [(i, *r) for i, r in enumerate(rows, start=1)])
        sync_identity_sequences(conn)

    rollup_rows = refresh_delay_metrics(conn)
    conn.execute("ANALYZE")
    collisions = conn.execute(
        "SELECT COUNT(*) FROM (SELECT tracking_number FROM shipments "
        "GROUP BY tracking_number HAVING COUNT(DISTINCT carrier_id) > 1) AS t"
    ).fetchone()[0]
    conn.close()
    shown = str(target).split("@")[-1]  # never echo credentials from a URL
    print(f"Seeded {shown} in {time.perf_counter() - t0:.1f}s: {n_suppliers:,} suppliers, "
          f"{n_shipments:,} shipments, {rollup_rows:,} rollup rows, "
          f"{collisions:,} tracking numbers shared across carriers")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=default_target(), help="SQLite path or postgresql:// URL")
    p.add_argument("--suppliers", type=int, default=1200)
    p.add_argument("--shipments", type=int, default=150_000)
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()
    seed(a.db, a.suppliers, a.shipments, a.seed)


if __name__ == "__main__":
    main()
