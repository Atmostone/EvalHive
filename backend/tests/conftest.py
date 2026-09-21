"""Shared fixtures for the EvalHive test suite.

Tests assume an externally-provided Postgres reachable via TEST_DATABASE_URL
(e.g. a `pytest` service container in CI, or a docker-compose'd Postgres locally).
We do NOT spin up testcontainers from inside this process to keep tests runnable
both on a developer machine and in GitHub Actions.

Each test runs inside a transaction that is rolled back at the end so the database
state stays clean across tests.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Override DATABASE_URL before app.* imports so models bind to the test DB.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://evalhive:password@localhost:5432/evalhive_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("JWT_SECRET", "test-secret-please-do-not-use-in-prod")

from app import database  # noqa: E402  (must come after env override)
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Apply Alembic migrations once for the whole test session (sync, alembic-internal)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("+asyncpg", ""))
    command.upgrade(cfg, "head")
    yield


@pytest_asyncio.fixture
async def _engine():
    """Per-test async engine on NullPool — connections live in this test's loop only."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    # Replace globals so app code uses our test DB for the duration of the test.
    prev_engine = database.engine
    prev_session = database.async_session
    database.engine = engine
    database.async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield engine
    finally:
        await engine.dispose()
        database.engine = prev_engine
        database.async_session = prev_session


@pytest_asyncio.fixture
async def db_session(_engine, _truncate) -> AsyncIterator[AsyncSession]:
    """A transactional session that rolls back after each test."""
    async_session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(_engine, _truncate) -> AsyncIterator[AsyncClient]:
    """Anonymous httpx client wired to the FastAPI app via ASGI."""
    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c


@pytest_asyncio.fixture
async def auth_client(client: AsyncClient) -> AsyncClient:
    """A client that registered a fresh user and is preauthenticated."""
    email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/api/auth/register",
        json={"email": email, "password": "password1234", "display_name": "Test"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    client.headers["Authorization"] = f"Bearer {payload['access_token']}"
    client.headers["X-Workspace-Id"] = payload["default_workspace_id"]
    return client


@pytest_asyncio.fixture
async def _truncate(_engine) -> AsyncIterator[None]:
    """Wipe everything except the migration-seeded admin user + default workspace, before each test."""
    async with _engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE TABLE webhook_deliveries, agent_events, chat_messages, "
            "tasks, template_versions, templates, knowledge_documents, "
            "memory_relations, memory_entities, scheduled_jobs, service_tokens, "
            "llm_models, providers, "
            "workspace_members, workspaces, users RESTART IDENTITY CASCADE;"
        ))
        await conn.execute(text(
            "INSERT INTO users (id, email, password_hash, display_name, is_active) "
            "VALUES ('00000000-0000-0000-0000-000000000001', 'admin@local', NULL, 'Admin', true) "
            "ON CONFLICT (email) DO NOTHING;"
        ))
        await conn.execute(text(
            "INSERT INTO workspaces (id, name, slug, created_by) "
            "VALUES ('00000000-0000-0000-0000-000000000002', 'Default', 'default', "
            "'00000000-0000-0000-0000-000000000001') ON CONFLICT (slug) DO NOTHING;"
        ))
        await conn.execute(text(
            "INSERT INTO workspace_members (id, user_id, workspace_id, role) "
            "VALUES ('00000000-0000-0000-0000-000000000003', "
            "'00000000-0000-0000-0000-000000000001', "
            "'00000000-0000-0000-0000-000000000002', 'owner') "
            "ON CONFLICT (user_id, workspace_id) DO NOTHING;"
        ))
    yield


@pytest_asyncio.fixture
async def default_model(db_session):
    """Seed a Provider + LLMModel in the default workspace and assign all three system
    model FKs to it. Returns the LLMModel id (UUID).

    Use this in tests that exercise paths needing a configured LLM (orchestrator,
    chat, memory_extractor) or a template with model_id set.
    """
    from app.models.provider import LLMModel, Provider
    from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace

    provider = Provider(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="test-provider",
        api_key="test-key",
        endpoint="http://test.local/v1",
    )
    db_session.add(provider)
    await db_session.flush()
    model = LLMModel(
        provider_id=provider.id,
        display_name="TestModel",
        api_name="test-model",
    )
    db_session.add(model)
    await db_session.flush()

    workspace = await db_session.get(Workspace, DEFAULT_WORKSPACE_ID)
    workspace.orchestrator_model_id = model.id
    workspace.chat_model_id = model.id
    workspace.memory_extractor_model_id = model.id
    await db_session.commit()
    await db_session.refresh(model)
    return model
