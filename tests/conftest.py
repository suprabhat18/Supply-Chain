"""Test fixtures, parametrized over database backends.

SQLite always runs. Postgres runs when TEST_DATABASE_URL points at a server whose
user may CREATE DATABASE (CI provides one; see .github/workflows/ci.yml), e.g.

    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres pytest

Each Postgres test gets a fresh database cloned from a seeded template, which is
the Postgres equivalent of copying the SQLite file.
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connect  # noqa: E402
from scripts.seed import seed  # noqa: E402

PG_URL = os.environ.get("TEST_DATABASE_URL")
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])
SEED_ARGS = dict(n_suppliers=120, n_shipments=8_000, rng_seed=7)
TEMPLATE_DB = "sa_test_template"


def _pg_admin():
    import psycopg
    return psycopg.connect(PG_URL, autocommit=True)


def _pg_url_for(dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(PG_URL)
    params["dbname"] = dbname
    return make_conninfo(**params)


@pytest.fixture(scope="session", params=BACKENDS)
def seeded_db(request, tmp_path_factory):
    """Seed once per backend per session. Returns ('sqlite', path) or ('postgres', template name)."""
    if request.param == "sqlite":
        path = tmp_path_factory.mktemp("db") / "test.db"
        seed(path, **SEED_ARGS)
        yield "sqlite", str(path)
        return
    with _pg_admin() as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEMPLATE_DB}")
    seed(_pg_url_for(TEMPLATE_DB), **SEED_ARGS)
    yield "postgres", TEMPLATE_DB
    with _pg_admin() as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE_DB} WITH (FORCE)")


@pytest.fixture
def backend(seeded_db) -> str:
    return seeded_db[0]


@pytest.fixture
def db(seeded_db, tmp_path):
    """Per-test copy so tests that write don't affect each other. Returns a connect() target."""
    kind, src = seeded_db
    if kind == "sqlite":
        dst = tmp_path / "t.db"
        shutil.copy(src, dst)
        yield str(dst)
        return
    name = "sa_test_" + os.urandom(4).hex()
    with _pg_admin() as admin:
        admin.execute(f"CREATE DATABASE {name} TEMPLATE {src}")
    yield _pg_url_for(name)
    with _pg_admin() as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def conn(db):
    c = connect(db)
    yield c
    c.close()


@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", db)
    from app.main import app
    return TestClient(app)
