"""SQLite-based test configuration for Zerg.

Uses in-memory SQLite with per-worker isolation for pytest-xdist.
"""
import atexit
import os
from pathlib import Path

# Set *before* any project imports so backend skips background services
os.environ["TESTING"] = "1"

# Disable single-tenant mode for tests - tests need multiple users for permission testing
os.environ["SINGLE_TENANT"] = "0"

# Crypto – provide deterministic Fernet key for tests *before* any zerg imports.
if not os.environ.get("FERNET_SECRET"):
    os.environ["FERNET_SECRET"] = "Mj7MFJspDPjiFBGHZJ5hnx70XAFJ_En6ofIEhn3BoXw="

import asyncio
import sys
import tempfile
from unittest.mock import MagicMock
from unittest.mock import patch

import dotenv
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Register custom CLI options
# ---------------------------------------------------------------------------
def pytest_addoption(parser):
    parser.addoption("--live-url", action="store", default="http://localhost:30080", help="Base URL for live server")
    parser.addoption("--live-token", action="store", help="JWT Token for live server (optional)")


# Disable LangSmith/LangChain tracing for all tests
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
# wheel present
# ---------------------------------------------------------------------------

if "cryptography" not in sys.modules:
    import types as _types

    _crypto_mod = _types.ModuleType("cryptography")
    _fernet_mod = _types.ModuleType("cryptography.fernet")

    class _FakeFernet:
        def __init__(self, _key):
            self._key = _key

        def encrypt(self, data: bytes):
            return data

        def decrypt(self, token: bytes):
            return token

    _fernet_mod.Fernet = _FakeFernet
    sys.modules["cryptography"] = _crypto_mod
    sys.modules["cryptography.fernet"] = _fernet_mod

# Mock the LangSmith client to prevent any actual API calls
mock_langsmith = MagicMock()
mock_langsmith_client = MagicMock()
mock_langsmith.Client.return_value = mock_langsmith_client
sys.modules["langsmith"] = mock_langsmith
sys.modules["langsmith.client"] = MagicMock()

# Load .env from monorepo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
_env_path = _REPO_ROOT / ".env"
if _env_path.exists():
    dotenv.load_dotenv(_env_path)
else:
    dotenv.load_dotenv()


# ---------------------------------------------------------------------------
# SQLite test database setup
# ---------------------------------------------------------------------------
# Each pytest-xdist worker gets its own SQLite file for isolation

_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")

# Create per-worker SQLite database file
_TEST_DB_DIR = Path(tempfile.gettempdir()) / "zerg_tests"
_TEST_DB_DIR.mkdir(exist_ok=True)
_TEST_DB_FILE = _TEST_DB_DIR / f"test_{_XDIST_WORKER}.db"

# Set DATABASE_URL for this test worker BEFORE importing zerg.database
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_FILE}"

import zerg.database as _db_mod
import zerg.routers.websocket as _ws_router
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.events import EventType
from zerg.events import event_bus
from zerg.models.models import Fiche
from zerg.models.models import FicheMessage
from zerg.models.models import Thread
from zerg.models.models import ThreadMessage
from zerg.services.scheduler_service import scheduler_service
from zerg.websocket.manager import topic_manager

# Create test engine and session factory
SQLALCHEMY_DATABASE_URL = f"sqlite:///{_TEST_DB_FILE}"
test_engine = make_engine(SQLALCHEMY_DATABASE_URL)
TestingSessionLocal = make_sessionmaker(test_engine)

# Override default engine/factory so all app code uses the test database
_db_mod.default_engine = test_engine
_db_mod.default_session_factory = TestingSessionLocal
_db_mod.get_session_factory = lambda: TestingSessionLocal

# Ensure websocket router uses the same session factory
_ws_router.get_session_factory = lambda: TestingSessionLocal

# Mock the OpenAI module before importing main app
# EXCEPTION: When EVAL_MODE=live, skip mocking to allow real API calls
_eval_mode_early = os.environ.get("EVAL_MODE", "hermetic")
if _eval_mode_early != "live":
    mock_openai = MagicMock()
    mock_client = MagicMock()
    mock_chat = MagicMock()
    mock_completions = MagicMock()

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

# Import langgraph for patching
import langchain_openai
import langgraph
import langgraph.graph
import langgraph.graph.message

from langchain_core.messages import AIMessage


class _StubLlm:
    """Stub LLM that returns deterministic response for both sync and async APIs."""

    def __init__(self, tools=None):
        self._tools = tools or []

    def _make_response(self, messages):
        """Generate a response based on bound tools and user message."""
        has_tool_response = False
        for msg in messages:
            msg_type = getattr(msg, "type", None)
            if msg_type == "tool":
                has_tool_response = True
                break

        if has_tool_response:
            return AIMessage(content="Task completed successfully.")

        user_content = ""
        for msg in reversed(messages):
            if hasattr(msg, "content") and hasattr(msg, "type") and msg.type == "human":
                content = msg.content
                if content and not content.strip().startswith("<current_time>"):
                    user_content = content
                    break

        if self._tools:
            import re

            tool_names = [t.name if hasattr(t, "name") else str(t) for t in self._tools]
            oikos_tools = {"spawn_commis", "list_commiss", "read_commis_result"}

            if oikos_tools.issubset(set(tool_names)) and user_content:
                user_lower = user_content.lower()

                tool_name = None
                if any(kw in user_lower for kw in ["list", "show", "recent"]):
                    tool_name = "list_commiss"
                elif any(kw in user_lower for kw in ["read", "result", "job"]):
                    tool_name = "read_commis_result"
                elif any(kw in user_lower for kw in ["spawn", "calculate", "delegate", "create"]):
                    tool_name = "spawn_commis"

                if tool_name:
                    tool_args = {}
                    if tool_name == "spawn_commis":
                        tool_args = {"task": user_content, "model": "gpt-5-mini"}
                    elif tool_name == "list_commiss":
                        tool_args = {"limit": 10}
                    elif tool_name == "read_commis_result":
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

    def invoke(self, messages, **_kwargs):
        return self._make_response(messages)

    async def ainvoke(self, messages, **_kwargs):
        return self._make_response(messages)

    async def invoke_async(self, messages, **_kwargs):
        return self._make_response(messages)


class _StubChatOpenAI:
    """Replacement for ChatOpenAI constructor used in tests."""

    def __init__(self, *args, **kwargs):
        pass

    def bind_tools(self, tools):
        return _StubLlm(tools=tools)


# Patch ChatOpenAI so no external network call happens
_eval_mode = os.environ.get("EVAL_MODE", "hermetic")
if _eval_mode != "live":
    langchain_openai.ChatOpenAI = _StubChatOpenAI

    _sre_module = sys.modules.get("zerg.services.oikos_react_engine")
    if _sre_module is not None:
        _sre_module.ChatOpenAI = _StubChatOpenAI

# Import app after all engine setup and mocks are in place
from zerg.main import app


@pytest.fixture(scope="session", autouse=True)
def disable_langsmith_tracing():
    """Fixture to disable LangSmith tracing for all tests."""
    with (
        patch("langsmith.client.Client") as mock_client,
        patch("langsmith._internal._background_thread.tracing_control_thread_func") as mock_thread,
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.sync_trace.return_value = MagicMock()
        mock_client_instance.trace.return_value = MagicMock()
        mock_client.return_value = mock_client_instance
        mock_thread.return_value = None

        with patch("langsmith.wrappers.wrap_openai") as mock_wrap:
            mock_wrap.return_value = lambda *args, **kwargs: args[0]
            yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_global_resources(request):
    """Ensure global resources are cleaned up after the session."""
    yield

    print("\nPerforming session cleanup...")

    topic_manager.active_connections.clear()
    topic_manager.topic_subscriptions.clear()
    topic_manager.client_topics.clear()
    print("Cleared topic_manager state.")

    try:
        event_bus.unsubscribe(EventType.FICHE_CREATED, topic_manager._handle_fiche_event)
        event_bus.unsubscribe(EventType.FICHE_UPDATED, topic_manager._handle_fiche_event)
        event_bus.unsubscribe(EventType.FICHE_DELETED, topic_manager._handle_fiche_event)
        event_bus.unsubscribe(EventType.THREAD_CREATED, topic_manager._handle_thread_event)
        event_bus.unsubscribe(EventType.THREAD_UPDATED, topic_manager._handle_thread_event)
        event_bus.unsubscribe(EventType.THREAD_DELETED, topic_manager._handle_thread_event)
        event_bus.unsubscribe(EventType.THREAD_MESSAGE_CREATED, topic_manager._handle_thread_event)
        print("Unsubscribed topic_manager from event_bus.")
    except Exception as e:
        print(f"Error during topic_manager unsubscribe: {e}")

    try:
        async def _stop_scheduler():
            await scheduler_service.stop()

        if scheduler_service._initialized:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_stop_scheduler())
                print("Scheduled scheduler service stop.")
            except RuntimeError:
                asyncio.run(_stop_scheduler())
                print("Stopped scheduler service.")
        else:
            print("Scheduler service was not initialized, skipping stop.")
    except Exception as e:
        print(f"Error stopping scheduler service during cleanup: {e}")

    print("Session cleanup complete.")


# ---------------------------------------------------------------------------
# Database schema management - create tables at session start
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _db_schema():
    """Create database tables once for the entire test session."""
    from sqlalchemy import text

    # Import agents models
    from zerg.models.agents import AgentsBase

    # Create all tables
    Base.metadata.create_all(bind=test_engine)
    AgentsBase.metadata.create_all(bind=test_engine)

    yield

    # Cleanup: delete test database file
    if os.environ.get("ZERG_KEEP_TEST_DB") != "1":
        try:
            _TEST_DB_FILE.unlink(missing_ok=True)
            # Also clean up WAL and SHM files
            Path(str(_TEST_DB_FILE) + "-wal").unlink(missing_ok=True)
            Path(str(_TEST_DB_FILE) + "-shm").unlink(missing_ok=True)
        except Exception:
            pass


def _truncate_all_tables(connection):
    """Delete all rows from all tables."""
    from sqlalchemy import text

    # Get all table names in dependency order (reverse for deletion)
    table_names = [t.name for t in reversed(Base.metadata.sorted_tables)]

    # Disable foreign keys temporarily for truncation
    connection.execute(text("PRAGMA foreign_keys = OFF"))
    for table in table_names:
        connection.execute(text(f'DELETE FROM "{table}"'))
    connection.execute(text("PRAGMA foreign_keys = ON"))
    connection.commit()


@pytest.fixture
def db_session(_db_schema):
    """Provide a clean database session for each test."""
    # TRUNCATE all tables BEFORE the test to ensure clean state
    with test_engine.connect() as conn:
        _truncate_all_tables(conn)

    db = TestingSessionLocal()

    # Seed a deterministic user with id=1 to satisfy FK constraints in tests
    try:
        from zerg.models.models import User

        dev = User(id=1, email="dev@local")
        db.add(dev)
        db.commit()
    except Exception:
        db.rollback()

    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client(db_session, auth_headers):
    """Create a FastAPI TestClient with the test database dependency and auth headers."""

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
    """Create a FastAPI TestClient without authentication headers."""

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
def unauthenticated_client_no_raise(db_session):
    """Create a FastAPI TestClient that returns error responses instead of raising."""

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
    """Create a FastAPI TestClient with WebSocket support."""

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
    """Returns a session factory using the test database."""

    def get_test_session():
        return db_session

    return get_test_session


# ---------------------------------------------------------------------------
# Model fixtures
# ---------------------------------------------------------------------------
from zerg.models_config import DEFAULT_MODEL_ID
from zerg.models_config import TEST_MODEL_ID

TEST_MODEL = DEFAULT_MODEL_ID
TEST_COMMIS_MODEL = TEST_MODEL_ID
TEST_MODEL_CHEAP = TEST_MODEL_ID


@pytest.fixture
def test_model():
    """Default model for test fiches."""
    return DEFAULT_MODEL_ID


@pytest.fixture
def test_commis_model():
    """Default model for test commis (lighter weight)."""
    return TEST_MODEL_ID


# ---------------------------------------------------------------------------
# Fixtures – generic user + fiche helpers
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
def sample_fiche(db_session, _dev_user):
    """Create a sample fiche in the database."""
    fiche = Fiche(
        owner_id=_dev_user.id,
        name="Test Fiche",
        system_instructions="System instructions for test fiche",
        task_instructions="This is a test fiche",
        model=DEFAULT_MODEL_ID,
        status="idle",
    )
    db_session.add(fiche)
    db_session.commit()
    db_session.refresh(fiche)
    return fiche


@pytest.fixture
def sample_messages(db_session, sample_fiche):
    """Create sample messages for the sample fiche."""
    messages = [
        FicheMessage(fiche_id=sample_fiche.id, role="system", content="You are a test assistant"),
        FicheMessage(fiche_id=sample_fiche.id, role="user", content="Hello, test assistant"),
        FicheMessage(fiche_id=sample_fiche.id, role="assistant", content="Hello, I'm the test assistant"),
    ]

    for message in messages:
        db_session.add(message)

    db_session.commit()
    return messages


@pytest.fixture
def sample_thread(db_session, sample_fiche):
    """Create a sample thread in the database."""
    thread = Thread(
        fiche_id=sample_fiche.id,
        title="Test Thread",
        active=True,
        fiche_state={"test_key": "test_value"},
        memory_strategy="buffer",
    )
    db_session.add(thread)
    db_session.commit()
    db_session.refresh(thread)
    return thread


@pytest.fixture
def sample_thread_messages(db_session, sample_thread):
    """Create sample messages for the sample thread."""
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
def auth_headers():
    """Return minimal headers dict for tests that inject auth."""
    return {"Authorization": "Bearer test-token"}


@pytest.fixture()
def db(db_session):
    """Provide a short alias for the shared test database session."""
    return db_session


@pytest.fixture()
def test_user(_dev_user):
    """Return the deterministic dev user for tests."""
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
    """Mock the StateGraph class from LangGraph directly."""
    with patch("langgraph.graph.StateGraph") as mock_state_graph:
        mock_graph = MagicMock()
        mock_state_graph.return_value = mock_graph
        compiled_graph = MagicMock()
        mock_graph.compile.return_value = compiled_graph
        yield mock_state_graph


@pytest.fixture
def mock_langchain_openai():
    """Mock the LangChain OpenAI integration."""
    with patch("langchain_openai.ChatOpenAI") as mock_chat_openai:
        mock_chat = MagicMock()
        mock_chat_openai.return_value = mock_chat
        yield mock_chat_openai


# ---------------------------------------------------------------------------
# Tool registry cleanup (autouse for every test)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_tool_registry():
    """Clear runtime-registered tools before & after each test."""
    from zerg.tools.registry import get_registry

    reg = get_registry()
    reg.clear_runtime_tools()
    yield
    reg.clear_runtime_tools()


# ---------------------------------------------------------------------------
# Cleanup: stop LLMAuditLogger so background task doesn't leak
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def _shutdown_llm_audit_logger():
    """Gracefully stop the LLM audit logger at the end of the test session."""
    yield

    try:
        from zerg.services.llm_audit import audit_logger

        async def _stop_audit_logger():
            await audit_logger.shutdown()

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_stop_audit_logger())
        except RuntimeError:
            asyncio.run(_stop_audit_logger())
    except Exception:
        pass
