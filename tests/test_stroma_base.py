"""
Tests for stroma/base.py — StromaModule ABC

Tests safe_read, safe_write, safe_increment, safe_tick, and the
contract that makes state corruption structurally impossible.
"""

import math
import pytest
from unittest.mock import MagicMock
from src.stroma.base import StromaModule


# ── Test fixture: concrete module ──────────────────────────────────────────────

class MockModule(StromaModule):
    """Concrete implementation for testing."""
    MODULE_NAME = "test_module"

    def __init__(self):
        super().__init__()
        self.tick_called = False
        self.last_broadcast = None

    def tick(self, sanguis, broadcast):
        self.tick_called = True
        self.last_broadcast = broadcast


class FailingModule(StromaModule):
    """Module that raises on tick — for error isolation testing."""
    MODULE_NAME = "failing_module"

    def tick(self, sanguis, broadcast):
        raise RuntimeError("Module crashed!")


class NoNameModule(StromaModule):
    """Module without MODULE_NAME — should fail to init."""
    MODULE_NAME = ""

    def tick(self, sanguis, broadcast):
        pass


def make_sanguis(state=None):
    """Create a mock StateEngine with get/set."""
    mock = MagicMock()
    _state = state or {}

    def _get(key, default=None):
        parts = key.split(".")
        d = _state
        for p in parts:
            if isinstance(d, dict) and p in d:
                d = d[p]
            else:
                return default
        return d

    def _set(key, value):
        parts = key.split(".")
        d = _state
        for p in parts[:-1]:
            if p not in d:
                d[p] = {}
            d = d[p]
        d[parts[-1]] = value

    mock.get = MagicMock(side_effect=_get)
    mock.set = MagicMock(side_effect=_set)
    mock._state = _state
    return mock


# ── MODULE_NAME enforcement ────────────────────────────────────────────────────

class TestModuleInit:
    def test_valid_module_name(self):
        m = MockModule()
        assert m.MODULE_NAME == "test_module"
        assert m._tick_count == 0
        assert m._errors == 0

    def test_empty_module_name_raises(self):
        with pytest.raises(ValueError, match="must define MODULE_NAME"):
            NoNameModule()

    def test_repr(self):
        m = MockModule()
        assert "test_module" in repr(m)
        assert "ticks=0" in repr(m)


# ── safe_read ──────────────────────────────────────────────────────────────────

class TestSafeRead:
    def test_reads_existing_value(self):
        m = MockModule()
        s = make_sanguis({"endocrine": {"cortisol": 0.5}})
        assert m.safe_read(s, "endocrine.cortisol", 0.0) == 0.5

    def test_missing_key_returns_default(self):
        m = MockModule()
        s = make_sanguis({})
        assert m.safe_read(s, "nonexistent.key", 0.42) == 0.42

    def test_none_value_returns_default(self):
        m = MockModule()
        s = make_sanguis({"test": {"val": None}})
        assert m.safe_read(s, "test.val", 0.7) == 0.7

    def test_nan_value_returns_default(self):
        m = MockModule()
        s = make_sanguis({"test": {"val": float("nan")}})
        result = m.safe_read(s, "test.val", 0.3)
        assert result == 0.3

    def test_inf_value_returns_default(self):
        m = MockModule()
        s = make_sanguis({"test": {"val": float("inf")}})
        assert m.safe_read(s, "test.val", 0.1) == 0.1

    def test_negative_inf_returns_default(self):
        m = MockModule()
        s = make_sanguis({"test": {"val": float("-inf")}})
        assert m.safe_read(s, "test.val", 0.0) == 0.0

    def test_exception_returns_default(self):
        m = MockModule()
        s = MagicMock()
        s.get = MagicMock(side_effect=Exception("boom"))
        assert m.safe_read(s, "broken", 0.5) == 0.5

    def test_reads_string_value(self):
        m = MockModule()
        s = make_sanguis({"mode": "ventral"})
        assert m.safe_read(s, "mode", "unknown") == "ventral"

    def test_reads_list_value(self):
        m = MockModule()
        s = make_sanguis({"items": [1, 2, 3]})
        assert m.safe_read(s, "items", []) == [1, 2, 3]

    def test_reads_zero_correctly(self):
        """Zero is a valid value, not a missing value."""
        m = MockModule()
        s = make_sanguis({"val": 0.0})
        assert m.safe_read(s, "val", 0.5) == 0.0


# ── safe_read_float ────────────────────────────────────────────────────────────

class TestSafeReadFloat:
    def test_reads_and_clamps_high(self):
        m = MockModule()
        s = make_sanguis({"val": 1.5})
        assert m.safe_read_float(s, "val", 0.0, 0.0, 1.0) == 1.0

    def test_reads_and_clamps_low(self):
        m = MockModule()
        s = make_sanguis({"val": -0.5})
        assert m.safe_read_float(s, "val", 0.0, 0.0, 1.0) == 0.0

    def test_passes_through_valid(self):
        m = MockModule()
        s = make_sanguis({"val": 0.6})
        assert m.safe_read_float(s, "val", 0.0, 0.0, 1.0) == 0.6

    def test_non_numeric_returns_default(self):
        m = MockModule()
        s = make_sanguis({"val": "not_a_number"})
        assert m.safe_read_float(s, "val", 0.3) == 0.3

    def test_nan_returns_default(self):
        m = MockModule()
        s = make_sanguis({"val": float("nan")})
        assert m.safe_read_float(s, "val", 0.5) == 0.5

    def test_coerces_int_to_float(self):
        m = MockModule()
        s = make_sanguis({"val": 1})
        result = m.safe_read_float(s, "val", 0.0, 0.0, 1.0)
        assert result == 1.0
        assert isinstance(result, float)


# ── safe_write ─────────────────────────────────────────────────────────────────

class TestSafeWrite:
    def test_writes_valid_float(self):
        m = MockModule()
        s = make_sanguis({})
        assert m.safe_write(s, "test.val", 0.5) is True
        s.set.assert_called_with("test.val", 0.5)

    def test_rejects_none(self):
        m = MockModule()
        s = make_sanguis({})
        assert m.safe_write(s, "test.val", None) is False
        s.set.assert_not_called()

    def test_rejects_nan(self):
        m = MockModule()
        s = make_sanguis({})
        assert m.safe_write(s, "test.val", float("nan")) is False
        s.set.assert_not_called()

    def test_rejects_inf(self):
        m = MockModule()
        s = make_sanguis({})
        assert m.safe_write(s, "test.val", float("inf")) is False
        s.set.assert_not_called()

    def test_rejects_negative_inf(self):
        m = MockModule()
        s = make_sanguis({})
        assert m.safe_write(s, "test.val", float("-inf")) is False
        s.set.assert_not_called()

    def test_clamps_float_high(self):
        m = MockModule()
        s = make_sanguis({})
        m.safe_write(s, "test.val", 1.5, min_val=0.0, max_val=1.0)
        s.set.assert_called_with("test.val", 1.0)

    def test_clamps_float_low(self):
        m = MockModule()
        s = make_sanguis({})
        m.safe_write(s, "test.val", -0.5, min_val=0.0, max_val=1.0)
        s.set.assert_called_with("test.val", 0.0)

    def test_passes_string_through(self):
        m = MockModule()
        s = make_sanguis({})
        assert m.safe_write(s, "mode", "ventral") is True
        s.set.assert_called_with("mode", "ventral")

    def test_passes_dict_through(self):
        m = MockModule()
        s = make_sanguis({})
        data = {"a": 1, "b": 2}
        assert m.safe_write(s, "data", data) is True
        s.set.assert_called_with("data", data)

    def test_clamps_int(self):
        m = MockModule()
        s = make_sanguis({})
        m.safe_write(s, "count", 150, min_val=0, max_val=100)
        s.set.assert_called_with("count", 100)

    def test_bool_not_clamped_as_int(self):
        """Booleans are technically ints but should not be clamped."""
        m = MockModule()
        s = make_sanguis({})
        assert m.safe_write(s, "flag", True) is True
        s.set.assert_called_with("flag", True)

    def test_exception_on_set_returns_false(self):
        m = MockModule()
        s = MagicMock()
        s.set = MagicMock(side_effect=Exception("disk full"))
        assert m.safe_write(s, "key", 0.5) is False
        assert m._errors == 1


# ── safe_write_clamped ─────────────────────────────────────────────────────────

class TestSafeWriteClamped:
    def test_convenience_clamp(self):
        m = MockModule()
        s = make_sanguis({})
        m.safe_write_clamped(s, "val", 1.5)
        s.set.assert_called_with("val", 1.0)

    def test_default_range_is_0_to_1(self):
        m = MockModule()
        s = make_sanguis({})
        m.safe_write_clamped(s, "val", -0.3)
        s.set.assert_called_with("val", 0.0)


# ── safe_increment ─────────────────────────────────────────────────────────────

class TestSafeIncrement:
    def test_increment_existing(self):
        m = MockModule()
        s = make_sanguis({"val": 0.5})
        m.safe_increment(s, "val", 0.2)
        # Should write 0.7
        s.set.assert_called()
        written = s.set.call_args[0][1]
        assert abs(written - 0.7) < 0.001

    def test_increment_clamps_at_ceiling(self):
        m = MockModule()
        s = make_sanguis({"val": 0.9})
        m.safe_increment(s, "val", 0.5)
        written = s.set.call_args[0][1]
        assert written == 1.0

    def test_decrement_clamps_at_floor(self):
        m = MockModule()
        s = make_sanguis({"val": 0.1})
        m.safe_increment(s, "val", -0.5)
        written = s.set.call_args[0][1]
        assert written == 0.0

    def test_increment_missing_key_starts_at_zero(self):
        m = MockModule()
        s = make_sanguis({})
        m.safe_increment(s, "new.key", 0.3)
        written = s.set.call_args[0][1]
        assert abs(written - 0.3) < 0.001


# ── safe_tick (error isolation) ────────────────────────────────────────────────

class TestSafeTick:
    def test_successful_tick(self):
        m = MockModule()
        s = make_sanguis({})
        result = m.safe_tick(s, {})
        assert result is True
        assert m.tick_called is True
        assert m._tick_count == 1
        assert m._last_tick_ts > 0

    def test_failing_tick_caught(self):
        m = FailingModule()
        s = make_sanguis({})
        result = m.safe_tick(s, {})
        assert result is False
        assert m._errors == 1
        assert m._tick_count == 0  # Tick didn't complete

    def test_multiple_failures_accumulate(self):
        m = FailingModule()
        s = make_sanguis({})
        m.safe_tick(s, {})
        m.safe_tick(s, {})
        m.safe_tick(s, {})
        assert m._errors == 3

    def test_broadcast_passed_through(self):
        m = MockModule()
        s = make_sanguis({})
        broadcast = {"emergent_states": ["FLOW"], "cortisol": 0.2}
        m.safe_tick(s, broadcast)
        assert m.last_broadcast == broadcast


# ── get_health ─────────────────────────────────────────────────────────────────

class TestHealth:
    def test_healthy_module(self):
        m = MockModule()
        h = m.get_health()
        assert h["module"] == "test_module"
        assert h["tick_count"] == 0
        assert h["errors"] == 0
        assert h["healthy"] is True

    def test_degraded_after_errors(self):
        m = FailingModule()
        s = make_sanguis({})
        for _ in range(10):
            m.safe_tick(s, {})
        h = m.get_health()
        assert h["errors"] == 10
        assert h["healthy"] is False


# ── on_cascade hook ────────────────────────────────────────────────────────────

class CascadeModule(StromaModule):
    MODULE_NAME = "cascade_test"

    def __init__(self):
        super().__init__()
        self.cascades_received = []

    def tick(self, sanguis, broadcast):
        pass

    def on_cascade(self, cascade_name, intensity):
        self.cascades_received.append((cascade_name, intensity))


class TestCascadeHook:
    def test_cascade_received(self):
        m = CascadeModule()
        m.on_cascade("CORTISOL", 0.8)
        m.on_cascade("DOPAMINE", 0.5)
        assert len(m.cascades_received) == 2
        assert m.cascades_received[0] == ("CORTISOL", 0.8)
        assert m.cascades_received[1] == ("DOPAMINE", 0.5)
