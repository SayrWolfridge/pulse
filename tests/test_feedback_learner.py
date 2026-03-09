"""
Tests for RL-lite FeedbackLearner (src/feedback_learner.py).

Covers:
- FeedbackEvent construction and serialisation
- FeedbackLearner.record(): EMA updates, rolling window enforcement
- FeedbackLearner.get_weight_adjustment(): range, neutral default
- FeedbackLearner.effective_weight(): floor clamping
- FeedbackLearner.get_stats(): schema, counts, success_rate
- FeedbackLearner.reset_drive(): clears history + EMA
- Persistence: save / load round-trip, corrupt file recovery
- prometheus_lines(): output format
- Integration: successive outcomes converge correctly
- Edge cases: unknown outcome, empty history, zero pressure
"""

import json
import math
import time
import pytest
from pathlib import Path
from unittest.mock import patch

from pulse.src.feedback_learner import (
    FeedbackLearner,
    FeedbackEvent,
    ALPHA,
    MAX_ADJUSTMENT,
    MIN_WEIGHT_FLOOR,
    WINDOW,
    OUTCOME_SCORES,
)


# ─── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path


@pytest.fixture
def learner(state_dir):
    return FeedbackLearner(state_dir)


# ─── FeedbackEvent ─────────────────────────────────────────────────────────────


class TestFeedbackEvent:
    def test_success_score(self):
        ev = FeedbackEvent(pressure=2.0, outcome="success")
        assert ev.score == 1.0

    def test_partial_score(self):
        ev = FeedbackEvent(pressure=1.0, outcome="partial")
        assert ev.score == 0.3

    def test_blocked_score(self):
        ev = FeedbackEvent(pressure=0.5, outcome="blocked")
        assert ev.score == 0.0

    def test_failure_score(self):
        ev = FeedbackEvent(pressure=3.0, outcome="failure")
        assert ev.score == -1.0

    def test_unknown_outcome_score(self):
        ev = FeedbackEvent(pressure=1.0, outcome="mystery")
        assert ev.score == 0.0

    def test_custom_timestamp(self):
        ts = 1700000000.0
        ev = FeedbackEvent(pressure=1.0, outcome="success", ts=ts)
        assert ev.ts == ts

    def test_auto_timestamp(self):
        before = time.time()
        ev = FeedbackEvent(pressure=1.0, outcome="success")
        after = time.time()
        assert before <= ev.ts <= after

    def test_to_dict_keys(self):
        ev = FeedbackEvent(pressure=2.5, outcome="success", ts=1000.0)
        d = ev.to_dict()
        assert set(d.keys()) == {"ts", "pressure", "outcome", "score"}
        assert d["pressure"] == 2.5
        assert d["outcome"] == "success"
        assert d["score"] == 1.0

    def test_from_dict_roundtrip(self):
        ev = FeedbackEvent(pressure=1.5, outcome="partial", ts=999.0)
        d = ev.to_dict()
        ev2 = FeedbackEvent.from_dict(d)
        assert ev2.ts == ev.ts
        assert ev2.pressure == ev.pressure
        assert ev2.outcome == ev.outcome
        assert ev2.score == ev.score

    def test_from_dict_unknown_outcome(self):
        d = {"ts": 0.0, "pressure": 1.0, "outcome": "weird", "score": 99.0}
        ev = FeedbackEvent.from_dict(d)
        # stored score from dict, not recomputed
        assert ev.score == 99.0


# ─── FeedbackLearner — core behaviour ─────────────────────────────────────────


class TestFeedbackLearnerRecord:
    def test_record_returns_multiplier(self, learner):
        m = learner.record("goals", 2.0, "success")
        assert isinstance(m, float)
        assert 0.7 <= m <= 1.3

    def test_record_creates_history(self, learner):
        learner.record("goals", 2.0, "success")
        assert len(learner._history["goals"]) == 1

    def test_record_caps_at_window(self, learner):
        for _ in range(WINDOW + 5):
            learner.record("goals", 1.0, "success")
        assert len(learner._history["goals"]) == WINDOW

    def test_record_ema_update_success(self, learner):
        # After one success: ema = ALPHA * 1.0 + (1-ALPHA) * 0.0
        learner.record("goals", 1.0, "success")
        expected = ALPHA * 1.0
        assert math.isclose(learner._ema["goals"], expected, rel_tol=1e-6)

    def test_record_ema_update_failure(self, learner):
        learner.record("goals", 1.0, "failure")
        expected = ALPHA * (-1.0)
        assert math.isclose(learner._ema["goals"], expected, rel_tol=1e-6)

    def test_record_ema_sequential(self, learner):
        # Two successes
        learner.record("goals", 1.0, "success")
        ema1 = ALPHA * 1.0
        learner.record("goals", 1.0, "success")
        ema2 = ALPHA * 1.0 + (1 - ALPHA) * ema1
        assert math.isclose(learner._ema["goals"], ema2, rel_tol=1e-6)

    def test_record_saves_state(self, learner, state_dir):
        learner.record("goals", 1.0, "success")
        state_file = state_dir / "feedback_learner.json"
        assert state_file.exists()

    def test_multiple_drives_independent(self, learner):
        learner.record("goals", 2.0, "success")
        learner.record("curiosity", 1.0, "failure")
        assert learner._ema["goals"] > 0
        assert learner._ema["curiosity"] < 0


# ─── FeedbackLearner — get_weight_adjustment ──────────────────────────────────


class TestGetWeightAdjustment:
    def test_neutral_default(self, learner):
        # No history → EMA is 0.0 → multiplier 1.0
        assert learner.get_weight_adjustment("unknown_drive") == 1.0

    def test_positive_ema_increases_multiplier(self, learner):
        learner._ema["goals"] = 1.0
        assert learner.get_weight_adjustment("goals") == pytest.approx(
            1.0 + MAX_ADJUSTMENT
        )

    def test_negative_ema_decreases_multiplier(self, learner):
        learner._ema["goals"] = -1.0
        assert learner.get_weight_adjustment("goals") == pytest.approx(
            1.0 - MAX_ADJUSTMENT
        )

    def test_multiplier_clamped_high(self, learner):
        learner._ema["goals"] = 999.0
        assert learner.get_weight_adjustment("goals") == pytest.approx(
            1.0 + MAX_ADJUSTMENT
        )

    def test_multiplier_clamped_low(self, learner):
        learner._ema["goals"] = -999.0
        assert learner.get_weight_adjustment("goals") == pytest.approx(
            1.0 - MAX_ADJUSTMENT
        )

    def test_partial_ema_proportional(self, learner):
        learner._ema["goals"] = 0.5
        expected = 1.0 + 0.5 * MAX_ADJUSTMENT
        assert learner.get_weight_adjustment("goals") == pytest.approx(expected)


# ─── FeedbackLearner — effective_weight ───────────────────────────────────────


class TestEffectiveWeight:
    def test_neutral_returns_base_weight(self, learner):
        # No history → multiplier 1.0 → effective == base
        assert learner.effective_weight("goals", 1.5) == pytest.approx(1.5)

    def test_positive_ema_increases_weight(self, learner):
        learner._ema["goals"] = 1.0  # full reward
        assert learner.effective_weight("goals", 1.0) > 1.0

    def test_negative_ema_decreases_weight(self, learner):
        learner._ema["goals"] = -1.0  # full penalty
        assert learner.effective_weight("goals", 1.0) < 1.0

    def test_floor_enforced(self, learner):
        # base weight tiny, full penalty → should hit floor
        learner._ema["goals"] = -1.0
        eff = learner.effective_weight("goals", 0.01)
        assert eff == pytest.approx(MIN_WEIGHT_FLOOR)

    def test_floor_not_exceeded_without_penalty(self, learner):
        eff = learner.effective_weight("goals", 0.5)
        assert eff >= MIN_WEIGHT_FLOOR


# ─── FeedbackLearner — get_stats ──────────────────────────────────────────────


class TestGetStats:
    def test_empty_stats(self, learner):
        stats = learner.get_stats()
        assert stats["total_events"] == 0
        assert stats["drives"] == {}

    def test_stats_after_record(self, learner):
        learner.record("goals", 2.0, "success")
        stats = learner.get_stats()
        assert stats["total_events"] == 1
        g = stats["drives"]["goals"]
        assert g["events"] == 1
        assert g["success_rate"] == 1.0
        assert g["last_outcome"] == "success"

    def test_success_rate_mixed(self, learner):
        learner.record("goals", 1.0, "success")
        learner.record("goals", 1.0, "failure")
        stats = learner.get_stats()
        # 1 out of 2 = 0.5
        assert stats["drives"]["goals"]["success_rate"] == 0.5

    def test_success_rate_partial_counts(self, learner):
        learner.record("goals", 1.0, "partial")
        learner.record("goals", 1.0, "blocked")
        stats = learner.get_stats()
        # partial counts as success (1/2 = 0.5)
        assert stats["drives"]["goals"]["success_rate"] == 0.5

    def test_multiplier_in_stats(self, learner):
        # Record enough successes to drive EMA clearly positive
        for _ in range(10):
            learner.record("goals", 1.0, "success")
        stats = learner.get_stats()
        assert stats["drives"]["goals"]["multiplier"] > 1.0

    def test_total_events_across_drives(self, learner):
        learner.record("goals", 1.0, "success")
        learner.record("curiosity", 1.0, "success")
        learner.record("curiosity", 1.0, "partial")
        stats = learner.get_stats()
        assert stats["total_events"] == 3


# ─── FeedbackLearner — reset_drive ────────────────────────────────────────────


class TestResetDrive:
    def test_reset_clears_history(self, learner):
        learner.record("goals", 1.0, "success")
        learner.reset_drive("goals")
        assert "goals" not in learner._history

    def test_reset_clears_ema(self, learner):
        learner.record("goals", 1.0, "success")
        learner.reset_drive("goals")
        assert "goals" not in learner._ema

    def test_reset_nonexistent_drive_noop(self, learner):
        # Should not raise
        learner.reset_drive("phantom")

    def test_reset_returns_neutral_multiplier(self, learner):
        learner.record("goals", 1.0, "failure")
        learner.reset_drive("goals")
        assert learner.get_weight_adjustment("goals") == 1.0


# ─── Persistence ──────────────────────────────────────────────────────────────


class TestPersistence:
    def test_roundtrip(self, state_dir):
        learner1 = FeedbackLearner(state_dir)
        learner1.record("goals", 2.0, "success")
        learner1.record("goals", 1.5, "partial")
        learner1.record("curiosity", 0.8, "blocked")

        learner2 = FeedbackLearner(state_dir)
        assert len(learner2._history.get("goals", [])) == 2
        assert len(learner2._history.get("curiosity", [])) == 1
        assert math.isclose(
            learner2._ema["goals"], learner1._ema["goals"], rel_tol=1e-6
        )

    def test_corrupt_file_recovers(self, state_dir):
        path = state_dir / "feedback_learner.json"
        path.write_text("{ not valid json }")
        # Should not raise
        learner = FeedbackLearner(state_dir)
        assert learner._history == {}
        assert learner._ema == {}

    def test_missing_file_starts_empty(self, state_dir):
        learner = FeedbackLearner(state_dir)
        assert learner._history == {}

    def test_atomic_write(self, state_dir):
        """Ensures .tmp is not left on disk after successful save."""
        learner = FeedbackLearner(state_dir)
        learner.record("goals", 1.0, "success")
        tmp = state_dir / "feedback_learner.tmp"
        assert not tmp.exists()

    def test_version_field_written(self, state_dir):
        learner = FeedbackLearner(state_dir)
        learner.record("goals", 1.0, "success")
        data = json.loads((state_dir / "feedback_learner.json").read_text())
        assert data["version"] == 1


# ─── prometheus_lines ─────────────────────────────────────────────────────────


class TestPrometheusLines:
    def test_empty_produces_empty_string(self, learner):
        lines = learner.prometheus_lines()
        assert lines == ""

    def test_lines_after_record(self, learner):
        learner.record("goals", 1.0, "success")
        output = learner.prometheus_lines()
        assert "pulse_learner_ema" in output
        assert "pulse_learner_multiplier" in output
        assert "pulse_learner_events" in output
        assert "pulse_learner_success_rate" in output
        assert 'drive="goals"' in output

    def test_multiple_drives_in_lines(self, learner):
        learner.record("goals", 1.0, "success")
        learner.record("curiosity", 0.5, "partial")
        output = learner.prometheus_lines()
        assert 'drive="goals"' in output
        assert 'drive="curiosity"' in output


# ─── integration — convergence ────────────────────────────────────────────────


class TestConvergence:
    def test_all_success_converges_upward(self, learner):
        for _ in range(WINDOW):
            learner.record("goals", 1.0, "success")
        adj = learner.get_weight_adjustment("goals")
        assert adj > 1.0

    def test_all_failure_converges_downward(self, learner):
        for _ in range(WINDOW):
            learner.record("goals", 1.0, "failure")
        adj = learner.get_weight_adjustment("goals")
        assert adj < 1.0

    def test_mixed_50_50_near_neutral(self, learner):
        for i in range(WINDOW):
            outcome = "success" if i % 2 == 0 else "failure"
            learner.record("goals", 1.0, outcome)
        # EMA should be near 0 but not exact (depends on ordering)
        adj = learner.get_weight_adjustment("goals")
        # Should be within ±0.15 of neutral 1.0
        assert abs(adj - 1.0) < 0.15

    def test_recovery_after_failure_streak(self, learner):
        """Drive weight can recover if outcomes improve."""
        for _ in range(5):
            learner.record("goals", 1.0, "failure")
        low_adj = learner.get_weight_adjustment("goals")
        for _ in range(10):
            learner.record("goals", 1.0, "success")
        high_adj = learner.get_weight_adjustment("goals")
        assert high_adj > low_adj
