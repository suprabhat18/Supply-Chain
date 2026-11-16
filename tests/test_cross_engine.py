"""Migration acceptance test: identical data in SQLite and Postgres must produce
identical API responses, for every analytics endpoint, on both the live and the
rollup paths. This is Phase 2's verification step from docs/SCALING.md.

Requires TEST_DATABASE_URL (skipped otherwise).
"""
import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")

RANGES = [{}, {"date_from": "2025-01-01", "date_to": "2025-06-30"},
          {"date_from": "2025-03-14", "date_to": "2025-03-20"}, {"date_from": "2026-02-01"}]
MONTH_RANGES = [{}, {"date_from": "2025-01-01", "date_to": "2025-06-30"}, {"date_from": "2024-02-01", "date_to": "2024-02-29"}]


@pytest.fixture(scope="module")
def clients(tmp_path_factory):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.conftest import SEED_ARGS, _pg_admin, _pg_url_for
    from scripts.seed import seed

    sqlite_path = str(tmp_path_factory.mktemp("x") / "x.db")
    seed(sqlite_path, **SEED_ARGS)
    with _pg_admin() as admin:
        admin.execute("DROP DATABASE IF EXISTS sa_cross WITH (FORCE)")
        admin.execute("CREATE DATABASE sa_cross")
    pg_url = _pg_url_for("sa_cross")
    seed(pg_url, **SEED_ARGS)

    def make(target):
        def get(path, **params):
            old = os.environ.get("DATABASE_URL")
            os.environ["DATABASE_URL"] = target
            try:
                r = TestClient(app).get(path, params=params)
            finally:
                if old is None:
                    os.environ.pop("DATABASE_URL", None)
                else:
                    os.environ["DATABASE_URL"] = old
            assert r.status_code == 200, (target.split("@")[-1], path, params, r.text)
            return r.json()
        return get

    yield make(sqlite_path), make(pg_url)
    with _pg_admin() as admin:
        admin.execute("DROP DATABASE IF EXISTS sa_cross WITH (FORCE)")


def norm(x):
    """Engine-neutral comparison: drop the 'source' echo, round floats."""
    if isinstance(x, dict):
        return {k: norm(v) for k, v in x.items() if k not in ("source", "metrics_refreshed_at")}
    if isinstance(x, list):
        return [norm(v) for v in x]
    if isinstance(x, float):
        return round(x, 6)
    return x


@pytest.mark.parametrize("source", ["live", "rollup"])
@pytest.mark.parametrize("rng", RANGES)
def test_analytics_identical(clients, source, rng):
    lite, pg = clients
    params = {**rng, "source": source}
    for path, extra in [("/analytics/summary", {"as_of": "2026-11-15"}),
                        ("/analytics/delays/by-carrier", {}),
                        ("/analytics/delays/trend", {}),
                        ("/analytics/delays/trend", {"carrier_id": 3})]:
        assert norm(lite(path, **params, **extra)) == norm(pg(path, **params, **extra)), path


@pytest.mark.parametrize("source", ["live", "rollup"])
@pytest.mark.parametrize("rng", MONTH_RANGES)
@pytest.mark.parametrize("order", ["worst", "best"])
def test_supplier_rankings_identical(clients, source, rng, order):
    lite, pg = clients
    params = {**rng, "source": source, "order": order, "limit": 500, "min_delivered": 5}
    a, b = lite("/analytics/delays/by-supplier", **params), pg("/analytics/delays/by-supplier", **params)
    assert len(a) > 10 and norm(a) == norm(b)


def test_shipments_and_reference_identical(clients):
    lite, pg = clients
    for path, params in [("/carriers", {}), ("/suppliers", {"limit": 500}), ("/suppliers", {"q": "apex"}),
                         ("/suppliers/1", {}), ("/suppliers/77", {}),
                         ("/shipments", {"limit": 300}), ("/shipments", {"late_only": True, "limit": 200}),
                         ("/shipments", {"carrier_scac": "OCMR", "status": "in_transit"}),
                         ("/shipments/4321", {})]:
        assert norm(lite(path, **params)) == norm(pg(path, **params)), path
