"""Conftest: a throwaway data dir, with a fresh database per test."""

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="syncledger-tests-")
os.environ.setdefault("DATA_DIR", _TMP)

import pytest  # noqa: E402

from app.config import DATA_DIR, DB_FILENAME  # noqa: E402
from app.seed import init_db  # noqa: E402


@pytest.fixture()
def client():
    db_path = DATA_DIR / DB_FILENAME
    if db_path.exists():
        db_path.unlink()
    init_db()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c
