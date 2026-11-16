import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connect  # noqa: E402
from scripts.seed import seed  # noqa: E402


@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory) -> Path:
    """A small but realistic dataset (seeded once per test session)."""
    path = tmp_path_factory.mktemp("db") / "test.db"
    seed(path, n_suppliers=120, n_shipments=8_000, rng_seed=7)
    return path


@pytest.fixture
def db(seeded_db, tmp_path) -> Path:
    """Per-test copy so tests that write don't affect each other."""
    import shutil
    dst = tmp_path / "t.db"
    shutil.copy(seeded_db, dst)
    return dst


@pytest.fixture
def conn(db):
    c = connect(db)
    yield c
    c.close()


@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SHIPMENTS_DB", str(db))
    from app.main import app
    return TestClient(app)
