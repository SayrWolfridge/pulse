"""Tests for RelationshipGraph — Pulse v2 Day 12."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pulse.src.runtime.context_engine import ContextEngine
from pulse.src.runtime.emotion_engine import EmotionEngine
from pulse.src.runtime.relationship_graph import RelationshipGraph
from pulse.src.runtime.state_engine import StateEngine


@pytest.fixture()
def runtime_parts(tmp_path: Path):
    state = StateEngine(tmp_path / "state.json")
    context = ContextEngine(tmp_path)
    emotion = EmotionEngine(state)
    graph = RelationshipGraph(context=context, state=state, emotion=emotion)
    return state, context, emotion, graph


def test_record_event_updates_bond_and_themes(runtime_parts):
    _, _, _, graph = runtime_parts

    rec = graph.record_event(person="Josh", kind="message", note="hi", themes=["pulse", "anima"])
    assert rec["person"] == "Josh"
    assert rec["bond_strength"] >= 0.5
    assert "pulse" in rec.get("recent_themes", [])

    rec2 = graph.record_event(person="Josh", kind="message", themes=["pulse", "convergence"])
    assert rec2["bond_strength"] >= rec["bond_strength"]
    themes = rec2.get("recent_themes", [])
    assert "convergence" in themes


def test_snapshot_sorted(runtime_parts):
    _, _, _, graph = runtime_parts

    graph.record_event(person="A", kind="message", delta_bond=0.01)
    graph.record_event(person="B", kind="message", delta_bond=0.20)

    snap = graph.snapshot(top=10)
    rels = snap["relationships"]
    assert len(rels) >= 2
    assert float(rels[0]["bond_strength"]) >= float(rels[1]["bond_strength"])


def test_reconnect_candidates(runtime_parts):
    _, context, _, graph = runtime_parts

    # Seed a relationship with old last_seen
    context.update_relationship(
        "OldFriend",
        {
            "bond_strength": 0.8,
            "last_seen": "2026-01-01T00:00:00+00:00",
            "_touch": False,
        },
    )

    cands = graph.reconnect_candidates(hours=24, min_bond=0.6)
    assert any(c["person"] == "OldFriend" for c in cands)
