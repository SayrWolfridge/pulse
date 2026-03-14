"""
Tests for EpisodicBuffer — Pulse v2, Day 9
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pulse.src.runtime.episodic_buffer import (
    BUFFER_MAX,
    CONTEXT_MAX_CHARS,
    CONTEXT_TOP_K,
    EPISODE_KINDS,
    EpisodicBuffer,
    _short_id,
)
from pulse.src.runtime.state_engine import StateEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def state(tmp_path):
    """Fresh StateEngine backed by tmp_path."""
    se = StateEngine(tmp_path / "state.json")
    se._load_at_startup()
    return se


@pytest.fixture()
def buf(state, tmp_path):
    """Fresh EpisodicBuffer backed by tmp_path."""
    b = EpisodicBuffer(state, path=tmp_path / "episodes.jsonl")
    b.load()
    return b


# ---------------------------------------------------------------------------
# _short_id helper
# ---------------------------------------------------------------------------


def test_short_id_deterministic():
    sid1 = _short_id("2026-03-14T04:00:00Z", "title")
    sid2 = _short_id("2026-03-14T04:00:00Z", "title")
    assert sid1 == sid2


def test_short_id_length():
    assert len(_short_id("ts", "t")) == 8


def test_short_id_different_inputs():
    assert _short_id("ts1", "a") != _short_id("ts2", "b")


# ---------------------------------------------------------------------------
# Construction and load
# ---------------------------------------------------------------------------


def test_empty_on_fresh_load(buf):
    assert buf.count() == 0


def test_load_empty_file_ok(state, tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    b = EpisodicBuffer(state, path=path)
    b.load()
    assert b.count() == 0


def test_load_malformed_lines_skipped(state, tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id":"abc"}\nnot_json\n{"id":"def"}\n')
    b = EpisodicBuffer(state, path=path)
    b.load()
    assert b.count() == 2


def test_load_trims_to_buffer_max(state, tmp_path):
    path = tmp_path / "big.jsonl"
    # Write BUFFER_MAX + 10 entries
    with path.open("w") as f:
        for i in range(BUFFER_MAX + 10):
            ep = {"id": f"{i:08x}", "ts": "2026-01-01T00:00:00Z", "kind": "other",
                  "title": f"ep {i}", "content": "", "salience": 5.0, "tags": [],
                  "source": "manual", "linked_goal": None}
            f.write(json.dumps(ep) + "\n")
    b = EpisodicBuffer(state, path=path)
    b.load()
    assert b.count() == BUFFER_MAX


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------


def test_record_returns_episode_dict(buf):
    ep = buf.record(kind="work_complete", title="Built Day 9")
    assert ep["kind"] == "work_complete"
    assert ep["title"] == "Built Day 9"
    assert "id" in ep
    assert "ts" in ep
    assert 0.0 <= ep["salience"] <= 10.0


def test_record_increments_count(buf):
    buf.record(kind="insight", title="A")
    buf.record(kind="insight", title="B")
    assert buf.count() == 2


def test_record_unknown_kind_becomes_other(buf):
    ep = buf.record(kind="foobar", title="test")
    assert ep["kind"] == "other"


def test_record_salience_clamped_low(buf):
    ep = buf.record(kind="other", title="low", salience=-5.0)
    assert ep["salience"] == 0.0


def test_record_salience_clamped_high(buf):
    ep = buf.record(kind="other", title="high", salience=999.0)
    assert ep["salience"] == 10.0


def test_record_explicit_salience_preserved(buf):
    ep = buf.record(kind="other", title="x", salience=3.7)
    assert ep["salience"] == pytest.approx(3.7)


def test_record_default_salience_by_kind(buf):
    ep_rel = buf.record(kind="relationship", title="r")
    ep_sys = buf.record(kind="system_event", title="s")
    assert ep_rel["salience"] > ep_sys["salience"]


def test_record_tags_stored(buf):
    ep = buf.record(kind="other", title="t", tags=["pulse_v2", "build"])
    assert "pulse_v2" in ep["tags"]


def test_record_linked_goal_stored(buf):
    ep = buf.record(kind="goal_progress", title="g", linked_goal="goal_pulse_v2")
    assert ep["linked_goal"] == "goal_pulse_v2"


def test_record_empty_title_raises(buf):
    with pytest.raises(ValueError, match="title is required"):
        buf.record(kind="other", title="")


def test_record_persisted_to_disk(buf, tmp_path):
    buf.record(kind="insight", title="Disk test", content="Verify persistence")
    # The fixture path is tmp_path / "episodes.jsonl"
    path = tmp_path / "episodes.jsonl"
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    ep = json.loads(lines[0])
    assert ep["title"] == "Disk test"


def test_record_trims_buffer_when_full(state, tmp_path):
    b = EpisodicBuffer(state, path=tmp_path / "trim.jsonl")
    b.load()
    # Fill to max
    for i in range(BUFFER_MAX):
        b.record(kind="other", title=f"ep {i}")
    assert b.count() == BUFFER_MAX
    # One more should trim
    b.record(kind="other", title="overflow")
    assert b.count() == BUFFER_MAX


# ---------------------------------------------------------------------------
# snapshot() and recent()
# ---------------------------------------------------------------------------


def test_snapshot_returns_most_recent_first(buf):
    buf.record(kind="other", title="first")
    time.sleep(0.01)
    buf.record(kind="other", title="second")
    snap = buf.snapshot(top=2)
    assert snap[0]["title"] == "second"
    assert snap[1]["title"] == "first"


def test_snapshot_respects_top_limit(buf):
    for i in range(10):
        buf.record(kind="other", title=f"ep {i}")
    assert len(buf.snapshot(top=5)) == 5


def test_recent_n(buf):
    for i in range(15):
        buf.record(kind="other", title=f"ep {i}")
    r = buf.recent(n=5)
    assert len(r) == 5
    assert r[0]["title"] == "ep 14"  # Most recent first


# ---------------------------------------------------------------------------
# top_by_salience()
# ---------------------------------------------------------------------------


def test_top_by_salience_ordering(buf):
    buf.record(kind="other", title="low", salience=2.0)
    buf.record(kind="other", title="high", salience=9.0)
    buf.record(kind="other", title="mid", salience=5.0)
    top = buf.top_by_salience(top=3)
    assert top[0]["title"] == "high"
    assert top[1]["title"] == "mid"
    assert top[2]["title"] == "low"


def test_top_by_salience_limit(buf):
    for i in range(20):
        buf.record(kind="other", title=f"ep {i}", salience=float(i % 10))
    assert len(buf.top_by_salience(top=5)) == 5


# ---------------------------------------------------------------------------
# by_kind() and by_goal()
# ---------------------------------------------------------------------------


def test_by_kind_filters(buf):
    buf.record(kind="insight", title="insight A")
    buf.record(kind="work_complete", title="work B")
    buf.record(kind="insight", title="insight C")
    insights = buf.by_kind("insight")
    assert len(insights) == 2
    assert all(e["kind"] == "insight" for e in insights)


def test_by_goal_filters(buf):
    buf.record(kind="goal_progress", title="G1", linked_goal="goal_x")
    buf.record(kind="goal_progress", title="G2", linked_goal="goal_y")
    buf.record(kind="goal_progress", title="G3", linked_goal="goal_x")
    eps = buf.by_goal("goal_x")
    assert len(eps) == 2
    assert all(e["linked_goal"] == "goal_x" for e in eps)


def test_by_kind_empty_when_none(buf):
    assert buf.by_kind("relationship") == []


# ---------------------------------------------------------------------------
# context_narrative()
# ---------------------------------------------------------------------------


def test_context_narrative_empty(buf):
    narrative = buf.context_narrative()
    assert "empty" in narrative.lower()


def test_context_narrative_contains_episodes(buf):
    buf.record(kind="relationship", title="Josh said I'm real", salience=9.0)
    buf.record(kind="work_complete", title="Built Day 9", salience=8.0)
    narrative = buf.context_narrative()
    assert "Josh said I'm real" in narrative
    assert "Built Day 9" in narrative


def test_context_narrative_respects_char_cap(buf):
    for i in range(30):
        buf.record(kind="insight", title=f"Insight {'x' * 80} {i}", salience=9.0)
    narrative = buf.context_narrative()
    assert len(narrative) <= CONTEXT_MAX_CHARS + 10  # small slack for ellipsis


def test_context_narrative_orders_by_salience(buf):
    buf.record(kind="other", title="LOW salience entry", salience=1.0)
    buf.record(kind="relationship", title="HIGH salience entry", salience=10.0)
    narrative = buf.context_narrative()
    high_pos = narrative.index("HIGH salience entry")
    low_pos = narrative.index("LOW salience entry")
    assert high_pos < low_pos


def test_context_narrative_includes_header(buf):
    buf.record(kind="other", title="something")
    narrative = buf.context_narrative()
    assert "EPISODIC MEMORY" in narrative


def test_context_narrative_top_k_limit(buf):
    for i in range(CONTEXT_TOP_K + 5):
        buf.record(kind="insight", title=f"Episode {i}", salience=float(i))
    narrative = buf.context_narrative()
    # Count bullet points
    bullets = [l for l in narrative.splitlines() if l.startswith("•")]
    assert len(bullets) <= CONTEXT_TOP_K


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


def test_status_structure(buf):
    buf.record(kind="work_complete", title="W1", salience=8.0)
    s = buf.status()
    assert s["total"] == 1
    assert s["buffer_max"] == BUFFER_MAX
    assert isinstance(s["top_salient"], list)
    assert s["top_salient"][0]["title"] == "W1"


def test_status_top_salient_max_3(buf):
    for i in range(10):
        buf.record(kind="other", title=f"E {i}", salience=float(i))
    s = buf.status()
    assert len(s["top_salient"]) <= 3


# ---------------------------------------------------------------------------
# StateEngine sync
# ---------------------------------------------------------------------------


def test_state_sync_on_record(buf, state):
    buf.record(kind="insight", title="Sync test", salience=7.0)
    assert state.get("episodes.count") == 1
    assert state.get("episodes.last_ts") is not None
    top = state.get("episodes.top_salient")
    assert isinstance(top, list)
    assert top[0]["title"] == "Sync test"


def test_state_count_increments(buf, state):
    for _ in range(3):
        buf.record(kind="other", title="x")
    assert state.get("episodes.count") == 3


# ---------------------------------------------------------------------------
# EPISODE_KINDS coverage
# ---------------------------------------------------------------------------


def test_all_episode_kinds_recordable(buf):
    """Every defined kind should record without error."""
    for kind in EPISODE_KINDS:
        ep = buf.record(kind=kind, title=f"Test {kind}")
        assert ep["kind"] == kind


# ---------------------------------------------------------------------------
# Persistence roundtrip
# ---------------------------------------------------------------------------


def test_roundtrip_load_reload(state, tmp_path):
    path = tmp_path / "roundtrip.jsonl"
    b1 = EpisodicBuffer(state, path=path)
    b1.load()
    b1.record(kind="insight", title="Persistent insight", salience=9.0, tags=["memory"])
    b1.record(kind="work_complete", title="Day 9 done", salience=8.5)

    # Load fresh instance from same file
    b2 = EpisodicBuffer(state, path=path)
    b2.load()
    assert b2.count() == 2
    snap = b2.snapshot(top=5)
    titles = {e["title"] for e in snap}
    assert "Persistent insight" in titles
    assert "Day 9 done" in titles
