"""Tests for AURA — Agent Unified Runtime Awareness."""

import json
import shutil
import time
from pathlib import Path

import pytest

# Monkey-patch AURA_DIR before import
_TEST_AURA_DIR = Path("/tmp/test-aura-" + str(int(time.time())))


def _patch_aura_dirs(monkeypatch):
    """Redirect AURA to a temp directory."""
    import pulse.src.runtime.aura as aura_mod
    monkeypatch.setattr(aura_mod, "AURA_DIR", _TEST_AURA_DIR)
    monkeypatch.setattr(aura_mod, "BROADCASTS_DIR", _TEST_AURA_DIR / "broadcasts")


@pytest.fixture(autouse=True)
def clean_aura_dir(monkeypatch):
    """Ensure clean temp dir for each test."""
    global _TEST_AURA_DIR
    _TEST_AURA_DIR = Path("/tmp/test-aura-" + str(int(time.monotonic_ns())))
    _patch_aura_dirs(monkeypatch)
    _TEST_AURA_DIR.mkdir(parents=True, exist_ok=True)
    yield
    shutil.rmtree(_TEST_AURA_DIR, ignore_errors=True)


class TestAuraBroadcaster:
    def test_broadcast_creates_file(self):
        from pulse.src.runtime.aura import AuraBroadcaster
        b = AuraBroadcaster("iris")
        event = b.broadcast("insight", {"content": "test insight"})
        assert event["agent"] == "iris"
        assert event["kind"] == "insight"
        assert "id" in event
        assert "ts" in event
        assert b._path.exists()

    def test_broadcast_invalid_kind_defaults_to_insight(self):
        from pulse.src.runtime.aura import AuraBroadcaster
        b = AuraBroadcaster("iris")
        event = b.broadcast("invalid_kind", {"content": "test"})
        assert event["kind"] == "insight"

    def test_broadcast_appends_to_file(self):
        from pulse.src.runtime.aura import AuraBroadcaster
        b = AuraBroadcaster("iris")
        b.broadcast("insight", {"content": "first"})
        b.broadcast("alert", {"content": "second"})
        lines = b._path.read_text("utf-8").strip().splitlines()
        assert len(lines) == 2


class TestAuraSubscriber:
    def test_poll_reads_other_agents(self):
        from pulse.src.runtime.aura import AuraBroadcaster, AuraSubscriber
        iris_b = AuraBroadcaster("iris")
        sage_sub = AuraSubscriber("sage")

        iris_b.broadcast("insight", {"content": "CPI cooling"})
        iris_b.broadcast("emotional_shift", {"from": "focused", "to": "excited"})

        events = sage_sub.poll()
        assert len(events) == 2
        assert events[0]["agent"] == "iris"
        assert events[0]["kind"] == "insight"

    def test_poll_skips_own_broadcasts(self):
        from pulse.src.runtime.aura import AuraBroadcaster, AuraSubscriber
        iris_b = AuraBroadcaster("iris")
        iris_sub = AuraSubscriber("iris")

        iris_b.broadcast("insight", {"content": "self-broadcast"})
        events = iris_sub.poll()
        assert len(events) == 0

    def test_poll_returns_no_duplicates(self):
        from pulse.src.runtime.aura import AuraBroadcaster, AuraSubscriber
        iris_b = AuraBroadcaster("iris")
        sage_sub = AuraSubscriber("sage")

        iris_b.broadcast("insight", {"content": "first"})
        events1 = sage_sub.poll()
        assert len(events1) == 1

        # Second poll — no new events
        events2 = sage_sub.poll()
        assert len(events2) == 0

        # New broadcast — should appear
        iris_b.broadcast("alert", {"content": "second"})
        events3 = sage_sub.poll()
        assert len(events3) == 1
        assert events3[0]["kind"] == "alert"

    def test_poll_skips_expired_events(self):
        from pulse.src.runtime.aura import AuraBroadcaster, AuraSubscriber
        iris_b = AuraBroadcaster("iris")
        sage_sub = AuraSubscriber("sage")

        # Broadcast with 0-hour TTL (immediately expired)
        event = iris_b.broadcast("insight", {"content": "expired"}, ttl_hours=0.0)
        # Wait a tiny bit to ensure age > 0
        time.sleep(0.01)
        events = sage_sub.poll()
        assert len(events) == 0

    def test_get_agent_state(self):
        from pulse.src.runtime.aura import AuraBroadcaster, AuraSubscriber
        iris_b = AuraBroadcaster("iris")
        sage_sub = AuraSubscriber("sage")

        iris_b.broadcast("insight", {"content": "not a summary"})
        iris_b.broadcast("state_summary", {"emotion": {"color": "curious"}, "goals_active": 3})

        state = sage_sub.get_agent_state("iris")
        assert state is not None
        assert state["emotion"]["color"] == "curious"
        assert state["goals_active"] == 3

    def test_get_agent_state_returns_none_if_no_summary(self):
        from pulse.src.runtime.aura import AuraBroadcaster, AuraSubscriber
        iris_b = AuraBroadcaster("iris")
        sage_sub = AuraSubscriber("sage")

        iris_b.broadcast("insight", {"content": "no summary here"})
        state = sage_sub.get_agent_state("iris")
        assert state is None

    def test_list_agents(self):
        from pulse.src.runtime.aura import AuraBroadcaster, AuraSubscriber
        AuraBroadcaster("iris").broadcast("insight", {"content": "a"})
        AuraBroadcaster("sage").broadcast("insight", {"content": "b"})

        vera_sub = AuraSubscriber("vera")
        agents = vera_sub.list_agents()
        assert "iris" in agents
        assert "sage" in agents
        assert "vera" not in agents


class TestAuraEngine:
    def test_full_flow(self):
        from pulse.src.runtime.aura import AuraEngine

        iris = AuraEngine("iris")
        sage = AuraEngine("sage")

        # Iris broadcasts
        iris.broadcast("insight", {"content": "CPI cooling faster than expected"})
        iris.broadcast_emotional_shift("focused", "excited", 0.8, 0.7)
        iris.broadcast_state_summary({"emotion": {"color": "excited"}, "goals_active": 2})

        # Sage polls
        events = sage.poll()
        assert len(events) == 3

        # Poll again — no new events
        events2 = sage.poll()
        assert len(events2) == 0

    def test_convenience_methods(self):
        from pulse.src.runtime.aura import AuraEngine

        iris = AuraEngine("iris")
        e1 = iris.broadcast_insight("test insight", source="test")
        assert e1["kind"] == "insight"
        assert e1["payload"]["content"] == "test insight"

        e2 = iris.broadcast_emotional_shift("calm", "excited", 0.9, 0.8)
        assert e2["kind"] == "emotional_shift"

        e3 = iris.broadcast_state_summary({"key": "val"})
        assert e3["kind"] == "state_summary"
        assert e3["ttl_hours"] == 1.0

    def test_snapshot(self):
        from pulse.src.runtime.aura import AuraEngine

        iris = AuraEngine("iris")
        snap = iris.snapshot()
        assert snap["agent"] == "iris"
        assert isinstance(snap["known_agents"], list)
        assert isinstance(snap["offsets"], dict)

    def test_multi_agent_communication(self):
        from pulse.src.runtime.aura import AuraEngine

        iris = AuraEngine("iris")
        sage = AuraEngine("sage")
        vera = AuraEngine("vera")

        # Iris and Sage broadcast
        iris.broadcast("insight", {"content": "from iris"})
        sage.broadcast("alert", {"content": "from sage"})

        # Vera sees both
        events = vera.poll()
        assert len(events) == 2
        agents = {e["agent"] for e in events}
        assert agents == {"iris", "sage"}

        # Iris sees Sage but not herself
        iris_events = iris.poll()
        assert len(iris_events) == 1
        assert iris_events[0]["agent"] == "sage"
