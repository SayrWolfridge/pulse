"""Tests for the Logos backlog engine."""

import json
import os
import tempfile
import time
import pytest
from unittest.mock import MagicMock

import sys
# Ensure pulse imports work
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from pulse.src.logos.schemas import Task, CANON, PLEROMA, AGORA, ARCHIVE
from pulse.src.logos.store import LogosStore
from pulse.src.logos.soma_bridge import SomaBridge
from pulse.src.logos.seed import seed, SEED_TASKS
from pulse.src.logos.messages import MessageStore, seed_messages, SEED_MESSAGES


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_backlog.db")
    s = LogosStore(db_path=db_path)
    yield s
    s.close()


@pytest.fixture
def sample_task():
    return Task(
        title="Test task",
        description="A test task for unit tests",
        project="gnosis",
        agent="mira",
        priority=3,
        tags=["test"],
    )


# --- Schema tests ---

def test_task_creation():
    task = Task(title="Do thing", description="desc", project="gnosis")
    assert task.id  # uuid generated
    assert task.status == "backlog"
    assert task.priority == 3
    assert task.agent == "mira"
    assert task.requires_human is False
    assert task.deploy_ready is False
    assert task.created_at > 0


def test_task_to_dict():
    task = Task(title="T", description="D", project="pulse", tags=["a", "b"])
    d = task.to_dict()
    assert d["title"] == "T"
    assert d["tags"] == ["a", "b"]
    assert "id" in d


def test_task_from_dict():
    d = {"title": "X", "description": "Y", "project": "anima", "priority": 5}
    task = Task.from_dict(d)
    assert task.title == "X"
    assert task.priority == 5


def test_task_from_dict_ignores_unknown():
    d = {"title": "X", "description": "Y", "project": "anima", "bogus_field": 99}
    task = Task.from_dict(d)
    assert task.title == "X"
    assert not hasattr(task, "bogus_field")


def test_symbolic_constants():
    assert CANON == "backlog"
    assert PLEROMA == "backlog"
    assert AGORA == "review"
    assert ARCHIVE == "done"


# --- Store tests ---

def test_create_and_get(store, sample_task):
    store.create_task(sample_task)
    retrieved = store.get_task(sample_task.id)
    assert retrieved is not None
    assert retrieved.title == "Test task"
    assert retrieved.tags == ["test"]


def test_get_nonexistent(store):
    assert store.get_task("nonexistent-id") is None


def test_update_task(store, sample_task):
    store.create_task(sample_task)
    updated = store.update_task(sample_task.id, title="Updated title", priority=5)
    assert updated.title == "Updated title"
    assert updated.priority == 5
    # Verify persistence
    retrieved = store.get_task(sample_task.id)
    assert retrieved.title == "Updated title"


def test_update_nonexistent(store):
    assert store.update_task("fake-id", title="X") is None


def test_status_transition_sets_started_at(store, sample_task):
    store.create_task(sample_task)
    assert sample_task.started_at is None
    updated = store.update_task(sample_task.id, status="in_progress")
    assert updated.started_at is not None


def test_status_transition_sets_completed_at(store, sample_task):
    store.create_task(sample_task)
    store.update_task(sample_task.id, status="in_progress")
    updated = store.update_task(sample_task.id, status="done")
    assert updated.completed_at is not None


def test_delete_task(store, sample_task):
    store.create_task(sample_task)
    assert store.delete_task(sample_task.id) is True
    assert store.get_task(sample_task.id) is None


def test_delete_nonexistent(store):
    assert store.delete_task("fake-id") is False


def test_list_tasks_all(store):
    for i in range(3):
        store.create_task(Task(title=f"Task {i}", description="", project="gnosis"))
    assert len(store.list_tasks()) == 3


def test_list_tasks_filter_project(store):
    store.create_task(Task(title="A", description="", project="gnosis"))
    store.create_task(Task(title="B", description="", project="anima"))
    assert len(store.list_tasks(project="gnosis")) == 1
    assert len(store.list_tasks(project="anima")) == 1


def test_list_tasks_filter_agent(store):
    store.create_task(Task(title="A", description="", project="gnosis", agent="mira"))
    store.create_task(Task(title="B", description="", project="gnosis", agent="iris"))
    assert len(store.list_tasks(agent="mira")) == 1


def test_list_tasks_filter_status(store):
    store.create_task(Task(title="A", description="", project="gnosis", status="backlog"))
    store.create_task(Task(title="B", description="", project="gnosis", status="done"))
    assert len(store.list_tasks(status="backlog")) == 1
    assert len(store.list_tasks(status="done")) == 1


def test_list_tasks_ordered_by_priority(store):
    store.create_task(Task(title="Low", description="", project="gnosis", priority=1))
    store.create_task(Task(title="High", description="", project="gnosis", priority=5))
    store.create_task(Task(title="Mid", description="", project="gnosis", priority=3))
    tasks = store.list_tasks()
    assert tasks[0].title == "High"
    assert tasks[1].title == "Mid"
    assert tasks[2].title == "Low"


def test_next_task(store):
    store.create_task(Task(title="Low", description="", project="gnosis", agent="mira", priority=1))
    store.create_task(Task(title="Critical", description="", project="gnosis", agent="mira", priority=5))
    store.create_task(Task(title="Other agent", description="", project="gnosis", agent="iris", priority=5))
    nxt = store.next_task("mira")
    assert nxt.title == "Critical"


def test_next_task_skips_non_backlog(store):
    t = Task(title="In progress", description="", project="gnosis", agent="mira", priority=5, status="in_progress")
    store.create_task(t)
    store.create_task(Task(title="Backlog", description="", project="gnosis", agent="mira", priority=1))
    nxt = store.next_task("mira")
    assert nxt.title == "Backlog"


def test_next_task_none(store):
    assert store.next_task("mira") is None


def test_stats(store):
    store.create_task(Task(title="A", description="", project="gnosis", agent="mira", status="backlog"))
    store.create_task(Task(title="B", description="", project="anima", agent="iris", status="done"))
    store.create_task(Task(title="C", description="", project="gnosis", agent="mira", status="blocked"))
    s = store.stats()
    assert s["total"] == 3
    assert s["by_project"]["gnosis"] == 2
    assert s["by_project"]["anima"] == 1
    assert s["by_agent"]["mira"] == 2
    assert s["by_status"]["backlog"] == 1
    assert s["by_status"]["done"] == 1


def test_is_empty(store):
    assert store.is_empty() is True
    store.create_task(Task(title="X", description="", project="gnosis"))
    assert store.is_empty() is False


# --- Soma bridge tests ---

def test_ingest_spec(store):
    spec = """
- Build the login page
- Add OAuth flow
- Write unit tests
    """
    bridge = SomaBridge(store=store)
    tasks = bridge.ingest_spec(spec, project="gnosis", agent="mira")
    assert len(tasks) == 3
    assert tasks[0].title == "Build the login page"
    assert tasks[0].project == "gnosis"


def test_ingest_spec_numbered(store):
    spec = """
1. First item
2. Second item
3. Third item
    """
    bridge = SomaBridge(store=store)
    tasks = bridge.ingest_spec(spec, project="anima")
    assert len(tasks) == 3
    assert tasks[2].title == "Third item"


def test_review_output_complete(store, sample_task):
    store.create_task(sample_task)
    bridge = SomaBridge(store=store)
    result = bridge.review_output(sample_task.id, "This is a complete output with sufficient detail and context for review")
    assert result.status == "review"
    assert result.requires_human is False


def test_review_output_incomplete_short(store, sample_task):
    store.create_task(sample_task)
    bridge = SomaBridge(store=store)
    result = bridge.review_output(sample_task.id, "Done")
    assert result.requires_human is True


def test_review_output_incomplete_todo(store, sample_task):
    store.create_task(sample_task)
    bridge = SomaBridge(store=store)
    result = bridge.review_output(sample_task.id, "Implemented the feature but TODO: add error handling for edge cases")
    assert result.requires_human is True


def test_review_output_incomplete_blocked(store, sample_task):
    store.create_task(sample_task)
    bridge = SomaBridge(store=store)
    result = bridge.review_output(sample_task.id, "Cannot proceed because this is blocked by missing API credentials")
    assert result.requires_human is True


def test_logos_pressure_empty(store):
    bridge = SomaBridge(store=store)
    assert bridge.get_logos_pressure() == 0.0


def test_logos_pressure_with_blocked(store):
    store.create_task(Task(title="Blocked", description="", project="gnosis", status="blocked"))
    bridge = SomaBridge(store=store)
    pressure = bridge.get_logos_pressure()
    assert 0.0 < pressure <= 1.0


def test_logos_pressure_with_critical_backlog(store):
    for i in range(5):
        store.create_task(Task(title=f"Critical {i}", description="", project="gnosis", priority=5))
    bridge = SomaBridge(store=store)
    pressure = bridge.get_logos_pressure()
    assert pressure > 0.3  # 5 critical tasks should generate real pressure


def test_logos_pressure_capped(store):
    # Create many blocked and critical tasks to push pressure past 1.0
    for i in range(20):
        store.create_task(Task(title=f"Blocked {i}", description="", project="gnosis", status="blocked"))
        store.create_task(Task(title=f"Critical {i}", description="", project="gnosis", priority=5))
    bridge = SomaBridge(store=store)
    assert bridge.get_logos_pressure() == 1.0


# --- Seed tests ---

def test_seed_populates_empty_db(store):
    count = seed(store)
    assert count == 16
    assert len(store.list_tasks()) == 16


def test_seed_skips_populated_db(store):
    store.create_task(Task(title="Existing", description="", project="gnosis"))
    count = seed(store)
    assert count == 0
    assert len(store.list_tasks()) == 1  # only the one we added


def test_seed_task_data():
    """Verify seed data has all required fields."""
    for td in SEED_TASKS:
        assert "title" in td
        assert "project" in td
        assert "agent" in td
        assert "priority" in td
        assert "tags" in td
        assert 1 <= td["priority"] <= 5


# --- API tests (mock HTTP) ---

@pytest.mark.asyncio
async def test_api_stats(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer
    from pulse.src.logos.api import LogosAPI

    store = LogosStore(db_path=str(tmp_path / "api_test.db"))
    store.create_task(Task(title="T1", description="", project="gnosis"))

    api = LogosAPI(store=store)
    app = web.Application()
    api.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/logos/stats")
        assert resp.status == 200
        data = await resp.json()
        assert data["total"] == 1

    store.close()


@pytest.mark.asyncio
async def test_api_create_and_get(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from pulse.src.logos.api import LogosAPI

    store = LogosStore(db_path=str(tmp_path / "api_test2.db"))
    api = LogosAPI(store=store)
    app = web.Application()
    api.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        # Create
        resp = await client.post("/logos/tasks", json={
            "title": "API task", "project": "pulse", "priority": 4
        })
        assert resp.status == 201
        data = await resp.json()
        task_id = data["id"]

        # Get
        resp = await client.get(f"/logos/tasks/{task_id}")
        assert resp.status == 200
        data = await resp.json()
        assert data["title"] == "API task"
        assert data["priority"] == 4

    store.close()


@pytest.mark.asyncio
async def test_api_list_filter(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from pulse.src.logos.api import LogosAPI

    store = LogosStore(db_path=str(tmp_path / "api_test3.db"))
    store.create_task(Task(title="G", description="", project="gnosis"))
    store.create_task(Task(title="A", description="", project="anima"))
    api = LogosAPI(store=store)
    app = web.Application()
    api.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/logos/tasks?project=gnosis")
        data = await resp.json()
        assert len(data) == 1
        assert data[0]["project"] == "gnosis"

    store.close()


@pytest.mark.asyncio
async def test_api_update_and_delete(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from pulse.src.logos.api import LogosAPI

    store = LogosStore(db_path=str(tmp_path / "api_test4.db"))
    task = Task(title="Del me", description="", project="gnosis")
    store.create_task(task)
    api = LogosAPI(store=store)
    app = web.Application()
    api.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        # Update
        resp = await client.patch(f"/logos/tasks/{task.id}", json={"status": "in_progress"})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "in_progress"

        # Delete
        resp = await client.delete(f"/logos/tasks/{task.id}")
        assert resp.status == 200

        # Verify gone
        resp = await client.get(f"/logos/tasks/{task.id}")
        assert resp.status == 404

    store.close()


@pytest.mark.asyncio
async def test_api_next_task(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from pulse.src.logos.api import LogosAPI

    store = LogosStore(db_path=str(tmp_path / "api_test5.db"))
    store.create_task(Task(title="Low", description="", project="gnosis", agent="mira", priority=1))
    store.create_task(Task(title="High", description="", project="gnosis", agent="mira", priority=5))
    api = LogosAPI(store=store)
    app = web.Application()
    api.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/logos/next/mira")
        assert resp.status == 200
        data = await resp.json()
        assert data["title"] == "High"

    store.close()


# --- Message store tests ---

@pytest.fixture
def msg_store(tmp_path):
    db_path = str(tmp_path / "test_messages.db")
    s = MessageStore(db_path=db_path)
    yield s
    s.close()


def test_send_and_get_inbox(msg_store):
    msg_id = msg_store.send_message("vera", "mira", "Test subject", "Test body")
    assert msg_id
    inbox = msg_store.get_inbox("mira")
    assert len(inbox) == 1
    assert inbox[0]["subject"] == "Test subject"
    assert inbox[0]["from_agent"] == "vera"


def test_mark_read(msg_store):
    msg_id = msg_store.send_message("sage", "iris", "Read me", "Body")
    assert msg_store.mark_read(msg_id) is True
    # Should not appear in unread inbox
    assert len(msg_store.get_inbox("iris")) == 0
    # Should appear with include_read
    assert len(msg_store.get_inbox("iris", include_read=True)) == 1


def test_mark_read_not_found(msg_store):
    assert msg_store.mark_read("nonexistent-id") is False


def test_inbox_includes_all_agent(msg_store):
    msg_store.send_message("sage", "all", "Broadcast", "Everyone sees this")
    assert len(msg_store.get_inbox("mira")) == 1
    assert len(msg_store.get_inbox("lyra")) == 1


def test_get_thread(msg_store):
    tid = "thread-123"
    msg_store.send_message("vera", "mira", "First", "Body 1", thread_id=tid)
    msg_store.send_message("mira", "vera", "Reply", "Body 2", thread_id=tid)
    thread = msg_store.get_thread(tid)
    assert len(thread) == 2
    assert thread[0]["subject"] == "First"
    assert thread[1]["subject"] == "Reply"


def test_get_all_messages(msg_store):
    for i in range(5):
        msg_store.send_message("vera", "mira", f"Msg {i}", "Body")
    all_msgs = msg_store.get_all_messages(limit=3)
    assert len(all_msgs) == 3


def test_seed_messages(tmp_path):
    db_path = str(tmp_path / "seed_msg.db")
    s = MessageStore(db_path=db_path)
    count = seed_messages(s)
    assert count == len(SEED_MESSAGES)
    # Idempotent
    assert seed_messages(s) == 0
    s.close()


def test_message_priority_ordering(msg_store):
    msg_store.send_message("vera", "iris", "Low", "Body", priority=1)
    msg_store.send_message("sage", "iris", "High", "Body", priority=5)
    inbox = msg_store.get_inbox("iris")
    assert inbox[0]["priority"] == 5
    assert inbox[1]["priority"] == 1


# --- Message API tests ---

@pytest.mark.asyncio
async def test_api_send_and_get_messages(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from pulse.src.logos.api import LogosAPI

    store = LogosStore(db_path=str(tmp_path / "msg_api.db"))
    msg_store_api = MessageStore(db_path=str(tmp_path / "msg_api.db"))
    api = LogosAPI(store=store, message_store=msg_store_api)
    app = web.Application()
    api.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        # Send
        resp = await client.post("/logos/messages", json={
            "from": "vera", "to": "mira", "subject": "API test", "body": "Hello"
        })
        assert resp.status == 201
        data = await resp.json()
        msg_id = data["id"]

        # Get inbox
        resp = await client.get("/logos/messages/mira")
        assert resp.status == 200
        msgs = await resp.json()
        assert len(msgs) == 1

        # Mark read
        resp = await client.patch(f"/logos/messages/{msg_id}/read")
        assert resp.status == 200

        # Unread inbox should be empty
        resp = await client.get("/logos/messages/mira")
        msgs = await resp.json()
        assert len(msgs) == 0

        # All messages endpoint
        resp = await client.get("/logos/messages/all")
        assert resp.status == 200
        msgs = await resp.json()
        assert len(msgs) == 1

    store.close()
    msg_store_api.close()


@pytest.mark.asyncio
async def test_api_send_message_validation(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from pulse.src.logos.api import LogosAPI

    store = LogosStore(db_path=str(tmp_path / "msg_val.db"))
    api = LogosAPI(store=store)
    app = web.Application()
    api.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/logos/messages", json={"from": "vera"})
        assert resp.status == 400

    store.close()
