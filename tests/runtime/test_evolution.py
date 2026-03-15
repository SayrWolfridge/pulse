"""Tests for pulse.runtime.evolution — Evolution + Guardrails + Mutator + Audit."""

import time

import pytest

from pulse.src.runtime.evolution import (
    Audit,
    Evolution,
    Guardrails,
    GuardrailViolation,
    MutationRecord,
    Mutator,
)
from pulse.src.runtime.state_engine import StateEngine


@pytest.fixture
def tmp_state(tmp_path):
    return StateEngine(tmp_path / "state.json")


class TestGuardrails:
    def test_valid_weight(self):
        g = Guardrails()
        assert g.validate("drives.goals.weight", 1.5) is True

    def test_weight_too_high(self):
        g = Guardrails()
        with pytest.raises(GuardrailViolation, match="exceeds max"):
            g.validate("drives.goals.weight", 5.0)

    def test_weight_too_low(self):
        g = Guardrails()
        with pytest.raises(GuardrailViolation, match="below min"):
            g.validate("drives.goals.weight", 0.01)

    def test_cannot_disable_immune(self):
        g = Guardrails()
        with pytest.raises(GuardrailViolation, match="Cannot disable"):
            g.validate("immune.enabled", False)

    def test_cannot_disable_amygdala(self):
        g = Guardrails()
        with pytest.raises(GuardrailViolation, match="Cannot disable"):
            g.validate("amygdala.enabled", False)

    def test_amygdala_sensitivity_floor(self):
        g = Guardrails()
        with pytest.raises(GuardrailViolation, match="sensitivity"):
            g.validate("amygdala.sensitivity", 0.1)

    def test_weight_delta_exceeded(self):
        g = Guardrails()
        with pytest.raises(GuardrailViolation, match="delta"):
            g.validate("drives.goals.weight", 2.0, current_value=0.5)

    def test_protected_drive_removal(self):
        g = Guardrails()
        with pytest.raises(GuardrailViolation, match="protected drive"):
            g.validate("drives.goals", None)

    def test_rate_limiting(self):
        g = Guardrails()
        # Fire many mutations
        for i in range(20):
            g.validate(f"test.field_{i}", i)
        # 21st should fail
        with pytest.raises(GuardrailViolation, match="rate limit"):
            g.validate("test.field_extra", 999)

    def test_status(self):
        g = Guardrails()
        status = g.status()
        assert "mutations_this_hour" in status
        assert "protected_modules" in status


class TestAudit:
    def test_record_and_recent(self, tmp_state):
        audit = Audit(tmp_state)
        audit.record(MutationRecord(
            timestamp=time.time(),
            target="drives.goals",
            before=0.5,
            after=0.7,
            reason="test mutation",
        ))
        recent = audit.recent(5)
        assert len(recent) == 1
        assert recent[0]["target"] == "drives.goals"
        assert recent[0]["hash"]  # tamper detection hash present

    def test_status(self, tmp_state):
        audit = Audit(tmp_state)
        audit.record(MutationRecord(
            timestamp=time.time(),
            target="test",
            before=1,
            after=2,
            reason="test",
        ))
        status = audit.status()
        assert status["total_mutations"] == 1
        assert status["recent_applied"] == 1


class TestMutator:
    def test_propose(self, tmp_state):
        tmp_state.set("drives.goals", 0.5)
        mutator = Mutator(tmp_state)
        proposal = mutator.propose("drives.goals", 0.7, "boost goals")
        assert proposal["current"] == 0.5
        assert proposal["proposed"] == 0.7

    def test_apply(self, tmp_state):
        tmp_state.set("drives.goals", 0.5)
        mutator = Mutator(tmp_state)
        old = mutator.apply("drives.goals", 0.7)
        assert old == 0.5
        assert tmp_state.get("drives.goals") == 0.7


class TestEvolution:
    def test_propose_mutation_applied(self, tmp_state):
        tmp_state.set("drives.curiosity", 0.5)
        evo = Evolution(tmp_state)
        result = evo.propose_mutation("drives.curiosity", 0.7, "boost curiosity")
        assert result is True
        assert tmp_state.get("drives.curiosity") == 0.7

    def test_propose_mutation_blocked(self, tmp_state):
        evo = Evolution(tmp_state)
        result = evo.propose_mutation("drives.goals.weight", 5.0, "too high")
        assert result is False

    def test_propose_mutation_blocked_immune(self, tmp_state):
        evo = Evolution(tmp_state)
        result = evo.propose_mutation("immune.enabled", False, "bad idea")
        assert result is False

    def test_audit_records_applied(self, tmp_state):
        tmp_state.set("drives.curiosity", 0.5)
        evo = Evolution(tmp_state)
        evo.propose_mutation("drives.curiosity", 0.7, "boost")
        recent = evo.audit.recent(5)
        assert len(recent) == 1
        assert recent[0]["applied"] is True

    def test_audit_records_blocked(self, tmp_state):
        evo = Evolution(tmp_state)
        evo.propose_mutation("drives.goals.weight", 5.0, "too high")
        recent = evo.audit.recent(5)
        assert len(recent) == 1
        assert recent[0]["applied"] is False

    def test_status(self, tmp_state):
        evo = Evolution(tmp_state)
        status = evo.status()
        assert "guardrails" in status
        assert "audit" in status

    def test_tick_with_plasticity_data(self, tmp_state):
        # Set up plasticity history with good outcomes
        history = {
            "curiosity": [
                {"ts": time.time() - i, "success": True, "quality": 0.8}
                for i in range(10)
            ]
        }
        tmp_state.set("plasticity.history", history)
        tmp_state.set("drives.curiosity", 0.5)

        evo = Evolution(tmp_state)
        evo.tick()

        # Should have proposed a weight increase
        new_val = tmp_state.get("drives.curiosity")
        assert new_val > 0.5  # increased

    def test_tick_with_bad_plasticity(self, tmp_state):
        # Set up plasticity history with poor outcomes
        history = {
            "system": [
                {"ts": time.time() - i, "success": False, "quality": 0.1}
                for i in range(10)
            ]
        }
        tmp_state.set("plasticity.history", history)
        tmp_state.set("drives.system", 0.5)

        evo = Evolution(tmp_state)
        evo.tick()

        # Should have proposed a weight decrease
        new_val = tmp_state.get("drives.system")
        assert new_val < 0.5  # decreased

    def test_tick_no_plasticity(self, tmp_state):
        evo = Evolution(tmp_state)
        evo.tick()  # should not raise
