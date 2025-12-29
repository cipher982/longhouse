import atexit
import os
from pathlib import Path

# Set *before* any project imports so backend skips background services
os.environ["TESTING"] = "1"

# Crypto – provide deterministic Fernet key for tests *before* any zerg imports.
# (Some modules import zerg.utils.crypto at import time.)
if not os.environ.get("FERNET_SECRET"):
    os.environ["FERNET_SECRET"] = "Mj7MFJspDPjiFBGHZJ5hnx70XAFJ_En6ofIEhn3BoXw="

import asyncio
import sys
from unittest.mock import MagicMock
from unittest.mock import patch

import dotenv
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Register custom CLI options for live integration tests
# ---------------------------------------------------------------------------
def pytest_addoption(parser):
    parser.addoption("--live-url", action="store", default="http://localhost:8000", help="Base URL for live server")
    parser.addoption("--live-token", action="store", help="JWT Token for live server (optional)")


import zerg.database as _db_mod
import zerg.routers.websocket as _ws_router
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.events import EventType
from zerg.events import event_bus
from zerg.models.models import Agent
from zerg.models.models import AgentMessage
from zerg.models.models import Thread
from zerg.models.models import ThreadMessage
from zerg.services.scheduler_service import scheduler_service
from zerg.websocket.manager import topic_manager

# Disable LangSmith/LangChain tracing for all tests.
# LangChain can treat the *presence* of these env vars as "enabled" (even if set to "false"),
# so we explicitly unset them to avoid tracer initialization during tests.
for _k in (
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_ENDPOINT",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_TRACING",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_API_KEY",
):
    os.environ.pop(_k, None)

# ---------------------------------------------------------------------------
# Stub *cryptography* so zerg.utils.crypto can import Fernet without the real
# wheel present.  We only need the API surface used in tests: ``encrypt`` and
# ``decrypt`` should round-trip the plaintext.
# ---------------------------------------------------------------------------

if "cryptography" not in sys.modules:  # guard in case real package installed
    import types as _types

    _crypto_mod = _types.ModuleType("cryptography")
    _fernet_mod = _types.ModuleType("cryptography.fernet")

    class _FakeFernet:  # noqa: D401 – minimal stub
        def __init__(self, _key):
            self._key = _key

        # Keep encryption a **no-op** inside the unit-test environment so
        # assertions that inspect raw values (e.g. auth tokens) still match.
        def encrypt(self, data: bytes):  # noqa: D401 – mimic API
            return data  # type: ignore[return-value]

        def decrypt(self, token: bytes):  # noqa: D401
            return token

    _fernet_mod.Fernet = _FakeFernet  # type: ignore[attr-defined]
    sys.modules["cryptography"] = _crypto_mod
    sys.modules["cryptography.fernet"] = _fernet_mod

# ---------------------------------------------------------------------------
# Crypto – note: FERNET_SECRET is set above, before any project imports.
# ---------------------------------------------------------------------------
# Mock the LangSmith client to prevent any actual API calls
mock_langsmith = MagicMock()
mock_langsmith_client = MagicMock()
mock_langsmith.Client.return_value = mock_langsmith_client
sys.modules["langsmith"] = mock_langsmith
sys.modules["langsmith.client"] = MagicMock()

# Load .env from monorepo root (5 levels up from conftest.py)
_REPO_ROOT = Path(__file__).resolve().parents[4]  # tests -> backend -> zerg -> apps -> repo_root
_env_path = _REPO_ROOT / ".env"
if _env_path.exists():
    dotenv.load_dotenv(_env_path)
else:
    # Tests can run without .env if all required env vars are already set
    dotenv.load_dotenv()


# Ensure docker-py can reach Docker Desktop on macOS where the socket lives
# under ~/.docker/run/docker.sock when /var/run/docker.sock is absent.
if not os.environ.get("DOCKER_HOST"):
    _sock = Path.home() / ".docker/run/docker.sock"
    if _sock.exists():
        os.environ["DOCKER_HOST"] = f"unix://{_sock}"

# Create a test database - always use ephemeral PostgreSQL via Testcontainers
from docker import from_env as _docker_from_env
from testcontainers.postgres import PostgresContainer

# Start a single Postgres container for the entire test session early so
# subsequent imports (e.g. routers) see the correct session factory.
try:
    _docker_from_env().ping()
except Exception as _e:
    raise RuntimeError(
        "Docker is required to run tests against PostgreSQL. Please install and start Docker Desktop (or provide a running Docker daemon)."
    ) from _e

# Configure Postgres for maximum test speed (unsafe for production!)
# These settings reduce durability guarantees in exchange for speed.
_pg_container = PostgresContainer(
    "postgres:16-alpine",
    driver="psycopg",
).with_command(
    "postgres "
    "-c fsync=off "                    # Don't sync to disk (huge speedup)
    "-c synchronous_commit=off "       # Don't wait for WAL write
    "-c full_page_writes=off "         # Skip full page writes after checkpoint
    "-c random_page_cost=1.1 "         # Assume fast storage (SSD/RAM)
    "-c effective_io_concurrency=200 " # Allow more parallel I/O
)
_pg_container.start()


# Register cleanup at interpreter exit - handles Ctrl+C, crashes, etc.
def _cleanup_pg_container():
    try:
        _pg_container.stop()
    except Exception:
        pass


atexit.register(_cleanup_pg_container)

# Prefer psycopg v3 driver in SQLAlchemy URL
SQLALCHEMY_DATABASE_URL = _pg_container.get_connection_url().replace("psycopg2", "psycopg")

# Create test engine and session factory bound to Postgres
from sqlalchemy.pool import NullPool

test_engine = make_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,
    poolclass=NullPool,
)

TestingSessionLocal = make_sessionmaker(test_engine)

# Override default engine/factory so all app code uses Postgres in tests
_db_mod.default_engine = test_engine
_db_mod.default_session_factory = TestingSessionLocal
_db_mod.get_session_factory = lambda: TestingSessionLocal  # type: ignore[assignment]

# Ensure websocket router uses the same Postgres session factory
_ws_router.get_session_factory = lambda: TestingSessionLocal  # type: ignore[attr-defined]

# Ensure *EmailTriggerService* uses the same Postgres session factory
try:
    from zerg.services.email_trigger_service import email_trigger_service as _ets  # noqa: WPS433 – test-time import

    _ets._session_factory = TestingSessionLocal  # type: ignore[attr-defined]
except ImportError:
    # In some collector runs the service may not be imported yet; tests that
    # need it will import later and patch manually via the fixture.
    pass

# Mock the OpenAI module before importing main app
mock_openai = MagicMock()
mock_client = MagicMock()
mock_chat = MagicMock()
mock_completions = MagicMock()

# Configure the mock to return a string for the content
mock_message = MagicMock()
mock_message.content = "This is a mock response from the LLM"
mock_choice = MagicMock()
mock_choice.message = mock_message
mock_choices = [mock_choice]
mock_response = MagicMock()
mock_response.choices = mock_choices

mock_completions.create.return_value = mock_response
mock_chat.completions = mock_completions
mock_client.chat = mock_chat
mock_openai.return_value = mock_client

sys.modules["openai"] = MagicMock()
sys.modules["openai.OpenAI"] = mock_openai

# Don't completely mock langgraph, just patch specific functionality
# This allows importing langgraph and accessing attributes like __version__
# while still mocking functionality used in tests
# First import the real modules to preserve their behavior
# The real modules are imported so we can selectively patch.
import langchain_openai  # noqa: E402
import langgraph  # noqa: E402
import langgraph.graph  # noqa: E402
import langgraph.graph.message  # noqa: E402

# ---------------------------------------------------------------------------
# Async-friendly stub for ChatOpenAI
# ---------------------------------------------------------------------------
from langchain_core.messages import AIMessage  # noqa: E402


class _StubLlm:
    """Stub LLM that returns deterministic response for both sync and async APIs.

    When tools are bound, returns a tool call for the first tool.
    When no tools are bound, returns a plain text response.
    """

    def __init__(self, tools=None):
        self._tools = tools or []

    def _make_response(self, messages):
        """Generate a response based on bound tools and user message."""
        # Check if there's already a tool response in the messages
        # If so, return a plain text response (task complete)
        has_tool_response = False
        for msg in messages:
            msg_type = getattr(msg, "type", None)
            if msg_type == "tool":
                has_tool_response = True
                break

        if has_tool_response:
            # Tool has been executed, return completion response
            return AIMessage(content="Task completed successfully.")

        # Look for the last human message (the actual user request, not system context)
        user_content = ""
        for msg in reversed(messages):
            if hasattr(msg, "content") and hasattr(msg, "type") and msg.type == "human":
                content = msg.content
                # Skip system context injection (starts with <current_time>)
                if content and not content.strip().startswith("<current_time>"):
                    user_content = content
                    break

        # Only return tool calls for tool integration tests (agents with ALL supervisor tools)
        # This prevents e2e tests from accidentally triggering background workers
        if self._tools:
            import re

            tool_names = [t.name if hasattr(t, "name") else str(t) for t in self._tools]
            supervisor_tools = {"spawn_worker", "list_workers", "read_worker_result"}

            # Only make tool calls if agent has ALL supervisor tools (tool integration tests)
            if supervisor_tools.issubset(set(tool_names)) and user_content:
                user_lower = user_content.lower()

                # Select tool based on keywords in user message
                tool_name = None
                if any(kw in user_lower for kw in ["list", "show", "recent"]):
                    tool_name = "list_workers"
                elif any(kw in user_lower for kw in ["read", "result", "job"]):
                    tool_name = "read_worker_result"
                elif any(kw in user_lower for kw in ["spawn", "calculate", "delegate", "create"]):
                    tool_name = "spawn_worker"

                if tool_name:
                    tool_args = {}
                    if tool_name == "spawn_worker":
                        tool_args = {"task": user_content, "model": "gpt-5-mini"}
                    elif tool_name == "list_workers":
                        tool_args = {"limit": 10}
                    elif tool_name == "read_worker_result":
                        match = re.search(r"job (\d+)", user_lower)
                        tool_args = {"job_id": int(match.group(1)) if match else 1}

                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "stub-tool-call-1",
                                "name": tool_name,
                                "args": tool_args,
                            }
                        ],
                    )

        return AIMessage(content="stub-response")

    def invoke(self, messages, **_kwargs):  # noqa: D401 – sync path used in production
        return self._make_response(messages)

    async def ainvoke(self, messages, **_kwargs):  # noqa: D401 – preferred async method
        return self._make_response(messages)

    async def invoke_async(self, messages, **_kwargs):  # noqa: D401 – legacy async method
        return self._make_response(messages)


class _StubChatOpenAI:
    """Replacement for ChatOpenAI constructor used in tests."""

    def __init__(self, *args, **kwargs):  # noqa: D401 – signature irrelevant
        pass

    def bind_tools(self, tools):  # noqa: D401
        return _StubLlm(tools=tools)


# Then patch specific classes or functions rather than entire modules
# ----------------------------------------------------------------------
# LangGraph / LLM stubs
# ----------------------------------------------------------------------


class _FakeRunnable:
    """Minimal runnable that returns a deterministic assistant reply."""

    def invoke(self, _state):  # noqa: D401 – interface mimic
        return {"messages": [AIMessage(content="stub-response")]}


class _FakeStateGraph(MagicMock):
    """Replacement for ``langgraph.graph.StateGraph`` used in tests.

    We inherit from MagicMock so existing attribute access patterns in the
    agent definition code still work (``add_node``, ``add_edge``, etc.), but
    we override ``compile`` to return our deterministic runnable instead of a
    plain MagicMock.  This removes the need for *any* fallback logic in
    production code.
    """

    def compile(self):  # noqa: D401 – mimic real API
        return _FakeRunnable()


# Patch the modules
# We now run the *real* LangGraph StateGraph implementation so the agent
# definition code executes unmodified.  We only patch *ChatOpenAI* because it
# performs an external network call.  ``add_messages`` is a pure helper; no
# need to mock it.

# Remove previous mocks if they were set by earlier lines in this file.
try:
    import importlib

    # Re-import the real sub-module to ensure we restored original symbols.
    importlib.reload(langgraph.graph)
    importlib.reload(langgraph.func)
    importlib.reload(langgraph.graph.message)
except Exception:  # pragma: no cover – defensive; tests still proceed
    pass

# Don't stub LangGraph StateGraph - let it execute real workflows
# We'll stub only the LLM calls within AgentRunner instead

# Patch ChatOpenAI so no external network call happens
# Replace with async-friendly stub so that code under test can *await* LLM responses.

langchain_openai.ChatOpenAI = _StubChatOpenAI  # type: ignore[attr-defined]

# Ensure already-imported agent definition module uses the stub as well.  If
# zerg.agents_def.zerg_react_agent was imported *before* this patch it will
# still hold a reference to the real ChatOpenAI class.  We overwrite it here
# defensively so no test ever triggers an external network call.

import sys as _sys

_zr_module = _sys.modules.get("zerg.agents_def.zerg_react_agent")
if _zr_module is not None:  # pragma: no cover – depends on import order
    _zr_module.ChatOpenAI = _StubChatOpenAI  # type: ignore[attr-defined]

# Don't mock AgentRunner globally - let individual tests mock it if needed

# Import app after all engine setup and mocks are in place
from zerg.main import app  # noqa: E402


# Stop the Postgres container after the entire test session completes
@pytest.fixture(scope="session", autouse=True)
def _shutdown_pg_container():
    yield
    try:
        _pg_container.stop()
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def disable_langsmith_tracing():
    """
    Fixture to disable LangSmith tracing for all tests.
    This is more robust than setting environment variables.
    """
    # First try patching internal classes that control tracing
    with (
        patch("langsmith.client.Client") as mock_client,
        patch("langsmith._internal._background_thread.tracing_control_thread_func") as mock_thread,
    ):
        # Disable tracing in the client
        mock_client_instance = MagicMock()
        mock_client_instance.sync_trace.return_value = MagicMock()
        mock_client_instance.trace.return_value = MagicMock()
        mock_client.return_value = mock_client_instance

        # Disable the background thread
        mock_thread.return_value = None

        # Also disable the tracing wrapper API
        with patch("langsmith.wrappers.wrap_openai") as mock_wrap:
            mock_wrap.return_value = lambda *args, **kwargs: args[0]
            yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_global_resources(request):
    """
    Ensure global resources like topic_manager are cleaned up after the session.
    This is crucial because topic_manager subscribes to event_bus at import time.
    """
    yield  # Run all tests

    # Teardown logic after all tests in the session have run
    print("\nPerforming session cleanup...")

    # 1. Clear topic_manager state
    #    Resetting internal dicts to break potential reference cycles
    #    and ensure no lingering client data.
    topic_manager.active_connections.clear()
    topic_manager.topic_subscriptions.clear()
    topic_manager.client_topics.clear()
    print("Cleared topic_manager state.")

    # 2. Unsubscribe topic_manager handlers from event_bus
    #    This is important to prevent errors if event_bus tries to call
    #    handlers on a potentially partially garbage-collected topic_manager.
    #    Assuming event_bus.unsubscribe is synchronous.
    try:
        event_bus.unsubscribe(EventType.AGENT_CREATED, topic_manager._handle_agent_event)
        event_bus.unsubscribe(EventType.AGENT_UPDATED, topic_manager._handle_agent_event)
        event_bus.unsubscribe(EventType.AGENT_DELETED, topic_manager._handle_agent_event)
        event_bus.unsubscribe(EventType.THREAD_CREATED, topic_manager._handle_thread_event)
        event_bus.unsubscribe(EventType.THREAD_UPDATED, topic_manager._handle_thread_event)
        event_bus.unsubscribe(EventType.THREAD_DELETED, topic_manager._handle_thread_event)
        event_bus.unsubscribe(EventType.THREAD_MESSAGE_CREATED, topic_manager._handle_thread_event)
        print("Unsubscribed topic_manager from event_bus.")
    except Exception as e:
        print(f"Error during topic_manager unsubscribe: {e}")

    # 3. Explicitly stop the scheduler service
    try:
        # Need to run the async stop method
        async def _stop_scheduler():
            await scheduler_service.stop()

        # Try to use existing event loop, otherwise create a new one
        # This handles both pytest-asyncio and non-async test scenarios
        if scheduler_service._initialized:
            try:
                # Try to get running loop first (for async test contexts)
                loop = asyncio.get_running_loop()
                # If we're in a running loop, schedule the stop as a task
                loop.create_task(_stop_scheduler())
                print("Scheduled scheduler service stop.")
            except RuntimeError:
                # No running loop, safe to use asyncio.run()
                asyncio.run(_stop_scheduler())
                print("Stopped scheduler service.")
        else:
            print("Scheduler service was not initialized, skipping stop.")
    except Exception as e:
        print(f"Error stopping scheduler service during cleanup: {e}")

    # 4. Optionally, clear event_bus subscribers if necessary (use with caution)
    # event_bus._subscribers.clear()
    # print("Cleared event_bus subscribers.")

    print("Session cleanup complete.")


# ---------------------------------------------------------------------------
# Database schema management - create once per session, TRUNCATE per test
# ---------------------------------------------------------------------------
# This is dramatically faster than CREATE/DROP per test (2572 DDL ops → 1 + fast TRUNCATEs)

@pytest.fixture(scope="session")
def _db_schema():
    """Create database schema once for the entire test session."""
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


def _truncate_all_tables(connection):
    """Truncate all tables efficiently using a single statement."""
    from sqlalchemy import text

    # Get all table names in dependency order (reverse for truncation)
    table_names = [t.name for t in reversed(Base.metadata.sorted_tables)]
    if not table_names:
        return

    # Use TRUNCATE ... CASCADE for speed (single statement, minimal WAL)
    # RESTART IDENTITY resets sequences for predictable IDs
    tables_str = ", ".join(table_names)
    connection.execute(text(f"TRUNCATE TABLE {tables_str} RESTART IDENTITY CASCADE"))
    connection.commit()


@pytest.fixture
def db_session(_db_schema):
    """
    Provide a clean database session for each test.

    Uses TRUNCATE instead of DROP/CREATE for massive speedup.
    Schema is created once per session, tables are truncated per test.
    """
    # TRUNCATE all tables BEFORE the test to ensure clean state
    with test_engine.connect() as conn:
        _truncate_all_tables(conn)

    db = TestingSessionLocal()

    # Seed a deterministic user with id=1 to satisfy FK constraints in tests
    try:
        from zerg.models.models import User
        from sqlalchemy import text

        dev = User(id=1, email="dev@local")
        db.add(dev)
        db.commit()

        # Advance sequence past seeded user so auto-generated IDs don't conflict
        # Uses MAX(id) so it self-heals if seed changes
        db.execute(text("SELECT setval('users_id_seq', COALESCE((SELECT MAX(id) FROM users), 1))"))
        db.commit()
    except Exception:
        # If seeding fails, continue; individual tests may create their own users
        db.rollback()

    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client(db_session, auth_headers):
    """
    Create a FastAPI TestClient with the test database dependency and auth headers.
    """

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, backend="asyncio") as client:
        client.headers = auth_headers
        yield client

    app.dependency_overrides = {}


@pytest.fixture
def unauthenticated_client(db_session):
    """
    Create a FastAPI TestClient without authentication headers.
    """

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, backend="asyncio", raise_server_exceptions=False) as client:
        yield client

    app.dependency_overrides = {}


@pytest.fixture
def test_client(db_session):
    """
    Create a FastAPI TestClient with WebSocket support
    """

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, backend="asyncio") as client:
        yield client

    app.dependency_overrides = {}


@pytest.fixture
def test_session_factory(db_session):
    """
    Returns a session factory using the test database.
    Used for cases where a service requires a session factory.
    Ensures all database operations in a test use the same connection.
    """

    def get_test_session():
        return db_session

    return get_test_session


# ---------------------------------------------------------------------------
# Model fixtures - centralized model constants for tests
# ---------------------------------------------------------------------------
from zerg.models_config import DEFAULT_MODEL_ID
from zerg.models_config import TEST_MODEL_ID

# Re-export as module-level constants for tests that need direct import
TEST_MODEL = DEFAULT_MODEL_ID  # "gpt-5.2" - for tests needing best quality
TEST_WORKER_MODEL = TEST_MODEL_ID  # "gpt-5-nano" - distinct model for allowlist tests
TEST_MODEL_CHEAP = TEST_MODEL_ID  # "gpt-5-nano" - for CI tests needing speed/cost


@pytest.fixture
def test_model():
    """Default model for test agents."""
    return DEFAULT_MODEL_ID


@pytest.fixture
def test_worker_model():
    """Default model for test workers (lighter weight)."""
    return DEFAULT_WORKER_MODEL_ID


# ---------------------------------------------------------------------------
# Fixtures – generic user + agent helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def _dev_user(db_session):
    """Return the deterministic *dev@local* user used when AUTH is disabled."""

    from zerg.crud import crud as _crud

    user = _crud.get_user_by_email(db_session, "dev@local")
    if user is None:
        user = _crud.create_user(db_session, email="dev@local", provider=None, role="USER")
    return user


@pytest.fixture
def sample_agent(db_session, _dev_user):
    """
    Create a sample agent in the database
    """
    agent = Agent(
        owner_id=_dev_user.id,
        name="Test Agent",
        system_instructions="System instructions for test agent",
        task_instructions="This is a test agent",
        model=DEFAULT_MODEL_ID,
        status="idle",
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


@pytest.fixture
def sample_messages(db_session, sample_agent):
    """
    Create sample messages for the sample agent
    """
    messages = [
        AgentMessage(agent_id=sample_agent.id, role="system", content="You are a test assistant"),
        AgentMessage(agent_id=sample_agent.id, role="user", content="Hello, test assistant"),
        AgentMessage(agent_id=sample_agent.id, role="assistant", content="Hello, I'm the test assistant"),
    ]

    for message in messages:
        db_session.add(message)

    db_session.commit()

    return messages


@pytest.fixture
def sample_thread(db_session, sample_agent):
    """
    Create a sample thread in the database
    """
    thread = Thread(
        agent_id=sample_agent.id,
        title="Test Thread",
        active=True,
        agent_state={"test_key": "test_value"},
        memory_strategy="buffer",
    )
    db_session.add(thread)
    db_session.commit()
    db_session.refresh(thread)
    return thread


@pytest.fixture
def sample_thread_messages(db_session, sample_thread):
    """
    Create sample messages for the sample thread
    """
    messages = [
        ThreadMessage(
            thread_id=sample_thread.id,
            role="system",
            content="You are a test assistant",
        ),
        ThreadMessage(
            thread_id=sample_thread.id,
            role="user",
            content="Hello, test assistant",
        ),
        ThreadMessage(
            thread_id=sample_thread.id,
            role="assistant",
            content="Hello, I'm the test assistant",
        ),
    ]

    for message in messages:
        db_session.add(message)

    db_session.commit()
    return messages


# ---------------------------------------------------------------------------
# HTTP helper fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_headers():  # noqa: D401 – pytest fixture naming
    """Return minimal **empty** headers dict for tests that inject auth.

    Most API endpoints depend on :pyfunc:`zerg.dependencies.auth.get_current_user`
    which, in *test mode*, bypasses JWT validation entirely (``AUTH_DISABLED``
    is implicitly enabled via the ``TESTING=1`` env flag set at the top of
    this file).  The tests for the MCP server router still expect a
    ``headers`` fixture so we provide a **no-op** implementation – an empty
    dict is sufficient because the middleware does not look at the headers
    in this scenario.
    """

    # Provide a dummy bearer token so routes that still check for the header
    # despite *AUTH_DISABLED* being in effect treat the request as
    # *authenticated*.  The token is **not** validated in dev-mode so the
    # value is irrelevant.

    return {"Authorization": "Bearer test-token"}


# Alias *db* fixture used in a handful of newer test files.  Internally we
# already expose a fully configured ``db_session`` fixture that yields a
# transactional SQLAlchemy session bound to the in-memory SQLite engine, so
# we simply return that reference for backwards compatibility.


@pytest.fixture()
def db(db_session):  # noqa: D401 – passthrough alias
    """Backward-compatibility shim – provide ``db`` as alias for *db_session*."""

    return db_session


# ---------------------------------------------------------------------------
# *test_user* – alias for legacy fixture name expected by a handful of new
# test modules.  Reuses the deterministic ``_dev_user`` helper.
# ---------------------------------------------------------------------------


@pytest.fixture()
def test_user(_dev_user):  # noqa: D401 – passthrough alias
    """Provide ``test_user`` as backwards-compatible alias for *_dev_user*."""

    return _dev_user


@pytest.fixture
def other_user(db_session):
    """Create a second, distinct user for isolation tests."""
    from zerg.crud import crud as _crud

    user = _crud.get_user_by_email(db_session, "other@local")
    if user is None:
        user = _crud.create_user(db_session, email="other@local", provider=None, role="USER")
    return user


@pytest.fixture
def mock_langgraph_state_graph():
    """
    Mock the StateGraph class from LangGraph directly.

    Note: Previously mocked from zerg.agents which has been removed.
    Now mocks langgraph.graph.StateGraph directly.
    """
    with patch("langgraph.graph.StateGraph") as mock_state_graph:
        # Create a mock graph
        mock_graph = MagicMock()
        mock_state_graph.return_value = mock_graph

        # Mock the compile method to return a graph instance
        compiled_graph = MagicMock()
        mock_graph.compile.return_value = compiled_graph

        yield mock_state_graph


@pytest.fixture
def mock_langchain_openai():
    """
    Mock the LangChain OpenAI integration
    """
    with patch("langchain_openai.ChatOpenAI") as mock_chat_openai:
        mock_chat = MagicMock()
        mock_chat_openai.return_value = mock_chat
        yield mock_chat_openai


# ---------------------------------------------------------------------------
# Cleanup: stop EmailTriggerService poll loop so pytest can exit immediately
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def _shutdown_email_trigger_service():
    """Ensure background poller is stopped at the end of the test session."""

    from zerg.services.email_trigger_service import email_trigger_service  # noqa: WPS433

    yield  # run tests

    # Cancel poll loop if still running (ignore event-loop already closed)
    try:

        async def _stop_email_service():
            await email_trigger_service.stop()

        try:
            # Try to get running loop first (for async test contexts)
            loop = asyncio.get_running_loop()
            # If we're in a running loop, schedule the stop as a task
            loop.create_task(_stop_email_service())
        except RuntimeError:
            # No running loop, safe to use asyncio.run()
            asyncio.run(_stop_email_service())
    except Exception:
        # Service already stopped or event-loop closed – no action needed
        pass


# ---------------------------------------------------------------------------
# Tool registry cleanup (autouse for every test)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_tool_registry():  # noqa: D401 – internal helper
    """Clear runtime-registered tools before & after each test.

    Several tests register mock tools on-the-fly.  Without cleanup later
    tests would see duplicates which either raise validation errors or –
    worse – make failures flaky and hard to trace.
    """

    from zerg.tools.registry import get_registry

    reg = get_registry()
    reg.clear_runtime_tools()
    yield
    reg.clear_runtime_tools()
