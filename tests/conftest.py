"""Test configuration.

Unit tests need nothing else. Integration tests need TEST_DATABASE_URL (set by `docker compose run tests`):
the schema is migrated once, the sample Jigsaw documents are ingested once with the offline fake models, and
each test gets fresh users and conversations.
"""

from __future__ import annotations

import os
import tempfile

# Settings are read at import time, so configure the environment before importing the application.
os.environ.update(
    APP_ENV="test",
    DATABASE_URL=os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://unused@localhost/unused"),
    STORAGE_DIR=tempfile.mkdtemp(prefix="jigsaw-test-uploads-"),
    OPENAI_API_KEY="sk-test-not-used",
    GROUNDING_JUDGE="true",
    RETRIEVAL_MIN_SIMILARITY="0.15",
    RERANKER_ENABLED="false",  # tests opt in with a fake (tests.fakes.OverlapReranker); no model download
    LOG_LEVEL="WARNING",
)

import pytest
from sqlalchemy import text

from app.rag import llm, rerank
from tests.fakes import FakeChatModel, HashEmbeddings

PASSWORD = "correct-horse-battery"
USERS = {
    "admin": "priya.shah@jigsaw-insurance.com",
    "underwriter": "tom.whitfield@jigsaw-insurance.com",
    "claims_handler": "aisha.okafor@jigsaw-insurance.com",
    "customer_service": "daniel.murphy@jigsaw-insurance.com",
}


@pytest.fixture(autouse=True)
def fake_models():
    chat = FakeChatModel()
    llm.use_models(chat, HashEmbeddings())
    yield chat
    llm.use_models(None, None)
    rerank.reset_reranker()


def _needs_db() -> None:
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("TEST_DATABASE_URL is not set (run: docker compose run --rm tests)")


@pytest.fixture(scope="session")
def database():
    """Migrated test database with the sample catalogue ingested (once per test run)."""
    _needs_db()
    from alembic import command
    from alembic.config import Config

    from app.cli import seed
    from app.config import PROJECT_ROOT
    from app.db import get_engine

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.downgrade(alembic_cfg, "base")  # also exercises the downgrade path
    command.upgrade(alembic_cfg, "head")
    llm.use_models(FakeChatModel(), HashEmbeddings())
    os.environ["SEED_PASSWORD"] = PASSWORD
    from app.config import get_settings

    get_settings.cache_clear()
    seed(wait=True)
    return get_engine()


@pytest.fixture
def db(database):
    from app.db import get_sessionmaker

    session = get_sessionmaker()()
    yield session
    session.close()
    with database.begin() as conn:  # each test starts with no conversations or sessions
        conn.execute(text("TRUNCATE conversations, sessions, feedback CASCADE"))
        conn.execute(text("UPDATE users SET failed_logins = 0, locked_until = NULL, is_active = true"))


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def sign_in(client, role: str):
    """Sign the test client in as the sample user with ``role`` (cookie session + CSRF header)."""
    r = client.post("/api/auth/login", json={"email": USERS[role], "password": PASSWORD})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return r.json()


@pytest.fixture
def as_role(client):
    def _sign_in(role: str):
        sign_in(client, role)
        return client

    return _sign_in
