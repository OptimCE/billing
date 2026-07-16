"""
Global test fixtures for Med2Go Admin Backend.

Infrastructure:
  pytest-docker   — spins up tests/docker-compose.test.yml at session start,
                    tears it down at session end. No manual `docker compose up`.

Schema:
  No Alembic yet. Schema is applied with psql directly from
  scripts/sql/schema.sql — the project's single source of truth.
  `Base.metadata.create_all()` is intentionally NOT used (it would miss
  check constraints, partial indexes, and any raw SQL DDL).

Auth:
  ENV=local activates set_auth_context_local as a global FastAPI dependency,
  which injects a hardcoded mock ADMIN user into ContextVars for every request.
  No Auth0 token or get_current_user override is needed.

Session isolation:
  Each test gets its own connection-level transaction that is rolled back on
  teardown — zero DB state leakage between tests.
  join_transaction_mode="create_savepoint" ensures that any session.commit()
  inside route handlers issues RELEASE SAVEPOINT instead of a real COMMIT,
  keeping the outer transaction open for the final rollback.
"""

import os
import socket
from collections.abc import AsyncGenerator
from pathlib import Path

# Force the test environment file (.env.test) BEFORE any project module is
# imported. core.config.Settings reads ENV at import time to choose .env.<env>,
# so this assignment must happen before `from main import app` below.
os.environ.setdefault("ENV", "test")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.database.database import get_crm_session, get_local_session
from main import app

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Must match docker-compose.test.yml: port 5433
TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/test_db_be"

# Absolute path to scripts/sql/schema.sql — the single source of truth for DDL
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
SCHEMA_SQL = os.path.normpath(os.path.join(_REPO_ROOT, "scripts", "sql", "schema.sql"))

# Test-only DDL for the CRM tables (community, community_subscription).
# The real CRM schema is owned by another service; we mirror only what tests
# in this repo need so the integration tests can hit a real Postgres.
CRM_TEST_SCHEMA_SQL = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "sql", "crm_test_schema.sql")
)


# ---------------------------------------------------------------------------
# pytest-docker — managed Postgres container
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_compose_file(pytestconfig):
    """Point pytest-docker at the test-only Compose file."""
    return os.path.join(str(pytestconfig.rootdir), "tests", "docker-compose.test.yml")


@pytest.fixture(scope="session")
def docker_compose_project_name():
    return "med2go-test"


def _is_pg_ready(host: str, port: int) -> bool:
    """Return True if Postgres is accepting TCP connections."""
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def test_db_ready(request):
    """Block until Postgres is accepting connections on port 5433.

    In CI (GitHub Actions), Postgres is provided by a service container and is
    already running — we just verify connectivity.  Locally, pytest-docker
    starts the container from docker-compose.test.yml first.
    """
    if os.getenv("CI"):
        # GitHub Actions: service container already running on 5433
        import time

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _is_pg_ready("localhost", 5433):
                return 5433
            time.sleep(0.5)
        raise RuntimeError("Postgres not ready on port 5433 after 30 s")

    # Local: delegate to pytest-docker
    docker_services = request.getfixturevalue("docker_services")
    docker_ip = request.getfixturevalue("docker_ip")
    port = docker_services.port_for("db-test", 5432)
    docker_services.wait_until_responsive(
        timeout=30.0,
        pause=0.5,
        check=lambda: _is_pg_ready(docker_ip, port),
    )
    return port


# ---------------------------------------------------------------------------
# Schema — applied once per session via psql + scripts/sql/schema.sql
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def apply_schema(test_db_ready):
    import asyncio

    import asyncpg

    schema_sql = Path(SCHEMA_SQL).read_text(encoding="utf-8")
    crm_test_schema_sql = Path(CRM_TEST_SCHEMA_SQL).read_text(encoding="utf-8")

    async def _apply():
        conn = await asyncpg.connect("postgresql://postgres:postgres@localhost:5433/test_db_be")
        await conn.execute(schema_sql)
        await conn.execute(crm_test_schema_sql)
        await conn.close()

    async def _teardown():
        conn = await asyncpg.connect("postgresql://postgres:postgres@localhost:5433/test_db_be")
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.close()

    asyncio.run(_apply())
    yield
    asyncio.run(_teardown())


# ---------------------------------------------------------------------------
# Engine — session-scoped sync fixture to avoid Windows event-loop teardown issues
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_engine(apply_schema):
    """
    Session-scoped SYNC fixture (not pytest_asyncio).

    Using a sync fixture prevents the Windows IocpProactor teardown bug where
    the session-scoped event loop is already closed by the time async fixture
    finalizers run, causing asyncpg to raise:
        AttributeError: 'NoneType' object has no attribute 'send'

    NullPool prevents SQLAlchemy from pooling connections between tests.
    With NullPool every `async with engine.connect()` opens a fresh asyncpg
    connection and closes it immediately on context exit — entirely within the
    test's own event loop. No background pool machinery runs after the loop
    closes, which eliminates the Windows IocpProactor error:
        AttributeError: 'NoneType' object has no attribute 'send'

    engine.dispose() is a no-op with NullPool (nothing to drain) but is kept
    for clarity. asyncio.run() creates its own fresh loop so teardown is safe.
    """
    import asyncio

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    yield engine
    asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# DB session — per-test, rolled back automatically
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """
    Wraps each test in a connection-level transaction rolled back on teardown.

    join_transaction_mode="create_savepoint" converts any session.commit() inside
    a route handler into RELEASE SAVEPOINT, leaving the outer transaction open so
    conn.rollback() cleanly undoes all inserts. Factories must flush(), not commit().
    """
    async with test_engine.connect() as conn:
        await conn.begin()
        factory = async_sessionmaker(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        session = factory()
        yield session
        await session.close()
        await conn.rollback()


# ---------------------------------------------------------------------------
# HTTP client — full stack, session injected, headers carry auth
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Full HTTP stack via ASGITransport (no real network; ASGI lifespan NOT triggered).

    Dependency overrides:
      get_crm_session → per-test rolled-back AsyncSession (isolates DB writes)

    Auth: this service relies on the KrakenD gateway forwarding x-user-id,
    x-community-id, and x-user-role headers; GatewayScopeMiddleware turns those
    into ContextVars which require_authenticated/require_community/require_min_role
    read directly. Tests therefore set these headers per request rather than
    overriding any FastAPI dependency.
    """

    async def _override_get_session():
        yield db_session

    app.dependency_overrides[get_crm_session] = _override_get_session
    # Generation routes use the Local DB. Tests run a single Postgres with
    # both schemas applied (scripts/sql/schema.sql + tests/sql/crm_test_schema.sql),
    # so the same per-test rolled-back session works for both bases.
    app.dependency_overrides[get_local_session] = _override_get_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
