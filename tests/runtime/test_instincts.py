"""Tests for pulse.runtime.instincts — InstinctRegistry + InstinctExecutor."""

import time

import pytest

from pulse.src.runtime.instincts import (
    Instinct,
    InstinctExecutor,
    InstinctOutput,
    InstinctRegistry,
    InstinctTrigger,
)
from pulse.src.runtime.state_engine import StateEngine


@pytest.fixture
def tmp_state(tmp_path):
    return StateEngine(tmp_path / "state.json")


class TestInstinctRegistry:
    def test_builtins_registered(self):
        registry = InstinctRegistry()
        instincts = registry.all_instincts()
        assert len(instincts) == 5
        names = {i.name for i in instincts}
        assert "high_frustration_rest" in names
        assert "josh_long_absence" in names
        assert "cascade_detected" in names
        assert "low_energy_recovery" in names
        assert "creative_surge" in names

    def test_register_custom(self):
        registry = InstinctRegistry()
        custom = Instinct(
            name="test_instinct",
            description="test",
            trigger=InstinctTrigger(name="test", check=lambda s: True),
            output=InstinctOutput(log_message="test fired"),
        )
        registry.register(custom)
        assert registry.get("test_instinct") is not None
        assert len(registry.all_instincts()) == 6

    def test_unregister(self):
        registry = InstinctRegistry()
        assert registry.unregister("high_frustration_rest") is True
        assert registry.unregister("nonexistent") is False
        assert len(registry.all_instincts()) == 4

    def test_status(self):
        registry = InstinctRegistry()
        status = registry.status()
        assert status["count"] == 5
        assert "instincts" in status
        assert "high_frustration_rest" in status["instincts"]


class TestInstinctExecutor:
    def test_no_triggers_fire_on_default_state(self, tmp_state):
        registry = InstinctRegistry()
        executor = InstinctExecutor(tmp_state, registry)
        state = tmp_state.snapshot()
        fired = executor.evaluate(state)
        # Default state shouldn't trigger any instincts
        assert isinstance(fired, list)

    def test_high_frustration_fires(self, tmp_state):
        registry = InstinctRegistry()
        executor = InstinctExecutor(tmp_state, registry)

        # Set up frustration state
        tmp_state.set("emotional_state.endocrine.cortisol", 8.0)
        tmp_state.set("emotional_state.valence", 0.2)

        state = tmp_state.snapshot()
        fired = executor.evaluate(state)
        names = [f["instinct"] for f in fired]
        assert "high_frustration_rest" in names

    def test_creative_surge_fires(self, tmp_state):
        registry = InstinctRegistry()
        executor = InstinctExecutor(tmp_state, registry)

        # Set up creative surge state
        tmp_state.set("emotional_state.endocrine.dopamine", 9.0)
        tmp_state.set("emotional_state.endocrine.serotonin", 9.0)
        tmp_state.set("emotional_state.valence", 0.8)

        state = tmp_state.snapshot()
        fired = executor.evaluate(state)
        names = [f["instinct"] for f in fired]
        assert "creative_surge" in names

    def test_cooldown_prevents_refire(self, tmp_state):
        registry = InstinctRegistry()
        executor = InstinctExecutor(tmp_state, registry)

        # Set up frustration state
        tmp_state.set("emotional_state.endocrine.cortisol", 8.0)
        tmp_state.set("emotional_state.valence", 0.2)

        state = tmp_state.snapshot()
        fired1 = executor.evaluate(state)
        assert len(fired1) > 0

        # Second evaluate should not fire (cooldown)
        fired2 = executor.evaluate(state)
        frustration_fired = [f for f in fired2 if f["instinct"] == "high_frustration_rest"]
        assert len(frustration_fired) == 0

    def test_fire_updates_state(self, tmp_state):
        registry = InstinctRegistry()
        executor = InstinctExecutor(tmp_state, registry)

        # Set up frustration state
        tmp_state.set("emotional_state.endocrine.cortisol", 8.0)
        tmp_state.set("emotional_state.valence", 0.2)

        state = tmp_state.snapshot()
        executor.evaluate(state)

        # Check state was updated with rest drive
        rest_drive = tmp_state.get("drives.rest")
        assert rest_drive is not None
        assert rest_drive == 0.8

    def test_custom_instinct_fires(self, tmp_state):
        registry = InstinctRegistry()
        custom = Instinct(
            name="always_fire",
            description="always fires",
            trigger=InstinctTrigger(name="always", check=lambda s: True),
            output=InstinctOutput(
                state_updates={"test.value": 42},
                log_message="always fired",
            ),
            cooldown_seconds=0,
        )
        registry.register(custom)
        executor = InstinctExecutor(tmp_state, registry)

        state = tmp_state.snapshot()
        fired = executor.evaluate(state)
        names = [f["instinct"] for f in fired]
        assert "always_fire" in names
        assert tmp_state.get("test.value") == 42

    def test_executor_status(self, tmp_state):
        registry = InstinctRegistry()
        executor = InstinctExecutor(tmp_state, registry)
        status = executor.status()
        assert "registry" in status
        assert status["registry"]["count"] == 5
