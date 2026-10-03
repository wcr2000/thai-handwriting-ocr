"""Fixtures shared by the tests that need a real Postgres.

Most tests in this project need no database, but the review queue (claiming slips and
preventing overwrites) cannot be tested with mocks: what is under test is the rowcount of a
conditional UPDATE and the behaviour of FOR UPDATE SKIP LOCKED, both of which are purely
Postgres semantics.

Point OCRSLIP_TEST_DATABASE_URL at a disposable database, or this group of tests is skipped.
"""

import os

import pytest

TEST_DB = os.getenv("OCRSLIP_TEST_DATABASE_URL", "")
TEST_SCHEMA = "ocr_test_queue"
TEST_PW = "pw-for-test"

needs_db = pytest.mark.skipif(
    not TEST_DB, reason="set OCRSLIP_TEST_DATABASE_URL to a Postgres whose data can be discarded"
)


import contextlib


@contextlib.contextmanager
def _pointed_at_test_db():
    """Point ocrslip's database target at the test database, always restoring the original.

    config.py reads .env (which points at production) at import time, and other modules do
    `from .config import DATABASE_URL, DB_SCHEMA`, binding those into names of their own. So
    setting an environment variable has no effect; the module attributes have to be overwritten
    directly.

    The originals must be restored at the end of each test, not at the end of the session: other
    test files (test_web_headers, test_review_queue) read the database at the real DATABASE_URL,
    and left pointing at the test database they would find synthetic rows with no images and
    fail.
    """
    from ocrslip import config, db, dedup, recheck, reprocess, search
    from ocrslip.web import pipeline

    targets = (config, db, dedup, search, recheck, reprocess, pipeline)
    saved = [(m, getattr(m, "DATABASE_URL", None), getattr(m, "DB_SCHEMA", None))
             for m in targets]
    for m in targets:
        if hasattr(m, "DATABASE_URL"):
            m.DATABASE_URL = TEST_DB
        if hasattr(m, "DB_SCHEMA"):
            m.DB_SCHEMA = TEST_SCHEMA
    # Last line of defence: if this ever ends up pointing elsewhere, die here, before a single
    # write statement runs.
    assert db.DATABASE_URL == TEST_DB, "db is not pointing at the test database"
    assert db.DB_SCHEMA == TEST_SCHEMA, "db is not using the test schema"
    try:
        yield
    finally:
        for m, url, schema in saved:
            if url is not None:
                m.DATABASE_URL = url
            if schema is not None:
                m.DB_SCHEMA = schema


@pytest.fixture(scope="session")
def _schema_ready():
    """Create the test schema once per session (init_schema issues dozens of statements)"""
    if not TEST_DB:
        pytest.skip("OCRSLIP_TEST_DATABASE_URL is not set")
    from ocrslip import auth, db

    with _pointed_at_test_db():
        db.init_schema()

    h = auth.hash_password(TEST_PW)
    for prefix, name in (("ADMIN", "admin"), ("USER", "staff"), ("APPROVE", "approve")):
        os.environ[f"{prefix}_USERNAME"] = name
        os.environ[f"{prefix}_PASSWORD_HASH"] = h


@pytest.fixture
def pgenv(_schema_ready):
    """Point ocrslip at the test database for the duration of this test only"""
    with _pointed_at_test_db():
        yield


@pytest.fixture
def make_slips(pgenv):
    """Create N pending slips, oldest first (the first is the head of the queue), clearing any existing rows"""
    from ocrslip.db import connect

    def _make(n: int = 3) -> list[str]:
        with connect() as conn:
            conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
            ids = [
                conn.execute(
                    f"""INSERT INTO {TEST_SCHEMA}.slips
                        (name, tel, plate_raw, review_status, needs_review, created_at)
                        VALUES (%s, %s, %s, 'pending', true, now() - (%s || ' min')::interval)
                        RETURNING id::text""",
                    (f"test {i}", f"08100000{i % 100:02d}", f"1กก{i}00{i}", 10_000 - i),
                ).fetchone()["id"]
                for i in range(n)
            ]
            conn.commit()
        return ids

    return _make


@pytest.fixture
def worker(pgenv):
    """One TestClient = one reviewer (each with its own worker cookie).

    Three people reviewing share one account, so the only thing distinguishing them is the worker
    cookie. Each TestClient has its own cookie jar, which models the real situation exactly.
    """
    from fastapi.testclient import TestClient

    from ocrslip import auth
    from ocrslip.web.main import app

    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()

    def _worker(username: str = "staff"):
        c = TestClient(app)
        r = c.post("/login", data={"username": username, "password": TEST_PW, "next": "/"},
                   follow_redirects=False)
        assert r.status_code == 303, f"login as {username} failed"
        c.get("/review")  # let the middleware issue the worker cookie first
        return c

    return _worker
