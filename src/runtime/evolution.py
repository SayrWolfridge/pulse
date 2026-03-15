"""
EVOLUTION — Self-Modification System
========================================
Ported from v1 pulse.src.evolution into v2 HypostasRuntime.

Handles how the system changes its own behavior over time.

Components:
  - Mutator:    applies approved mutations to drive weights, cycle intervals, behavior flags
  - Guardrails: prevents unsafe mutations
  - Audit:      logs every mutation with timestamp, reason, before/after values

Flow: Mutator.propose() → Guardrails.validate() → Mutator.apply() → Audit.record()

Wire: PLASTICITY → Evolution: when PLASTICITY identifies a drive that consistently
produces good outcomes, it proposes a mutation via Evolution.

All state persisted via StateEngine under ``evolution.*`` dot-paths.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .state_engine import StateEngine

logger = logging.getLogger("pulse.runtime.evolution")


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

class GuardrailViolation(Exception):
    """Raised when a mutation violates guardrails."""
    pass


class Guardrails:
    """Prevents unsafe mutations — the brainstem of self-modification."""

    # Hard limits
    MAX_DRIVE_WEIGHT = 3.0
    MIN_DRIVE_WEIGHT = 0.05
    MAX_WEIGHT_DELTA = 0.5
    MAX_MUTATIONS_PER_HOUR = 20
    MIN_AMYGDALA_SENSITIVITY = 0.3

    # Protected targets (cannot be disabled/zeroed)
    PROTECTED_MODULES = {"immune", "amygdala", "superego"}
    PROTECTED_DRIVES = {"goals", "emotions"}

    def __init__(self) -> None:
        self._mutation_timestamps: List[float] = []
        self._violations: int = 0

    def validate(self, target: str, new_value: Any, current_value: Any = None) -> bool:
        """Validate a proposed mutation. Returns True if allowed, raises on violation."""
        # Rate limiting
        self._check_rate()

        # Protected module check
        target_lower = target.lower()
        for mod in self.PROTECTED_MODULES:
            if f"{mod}.enabled" in target_lower and new_value is False:
                self._violations += 1
                raise GuardrailViolation(
                    f"Cannot disable protected module '{mod}'"
                )

        # Drive weight bounds
        if "weight" in target_lower or target_lower.startswith("drives."):
            if isinstance(new_value, (int, float)):
                if new_value > self.MAX_DRIVE_WEIGHT:
                    self._violations += 1
                    raise GuardrailViolation(
                        f"Drive weight {new_value} exceeds max {self.MAX_DRIVE_WEIGHT}"
                    )
                if new_value < self.MIN_DRIVE_WEIGHT:
                    self._violations += 1
                    raise GuardrailViolation(
                        f"Drive weight {new_value} below min {self.MIN_DRIVE_WEIGHT}"
                    )

        # AMYGDALA sensitivity floor
        if "amygdala" in target_lower and "sensitivity" in target_lower:
            if isinstance(new_value, (int, float)) and new_value < self.MIN_AMYGDALA_SENSITIVITY:
                self._violations += 1
                raise GuardrailViolation(
                    f"AMYGDALA sensitivity {new_value} below minimum {self.MIN_AMYGDALA_SENSITIVITY}"
                )

        # Weight delta check
        if current_value is not None and isinstance(new_value, (int, float)) and isinstance(current_value, (int, float)):
            delta = abs(new_value - current_value)
            if "weight" in target_lower and delta > self.MAX_WEIGHT_DELTA:
                self._violations += 1
                raise GuardrailViolation(
                    f"Weight delta {delta:.2f} exceeds max {self.MAX_WEIGHT_DELTA}"
                )

        # Protected drive removal
        for drive in self.PROTECTED_DRIVES:
            if target_lower == f"drives.{drive}" and new_value is None:
                self._violations += 1
                raise GuardrailViolation(f"Cannot remove protected drive '{drive}'")

        return True

    def _check_rate(self) -> None:
        """Ensure mutation rate limit is not exceeded."""
        now = time.time()
        one_hour_ago = now - 3600
        self._mutation_timestamps = [
            t for t in self._mutation_timestamps if t > one_hour_ago
        ]
        if len(self._mutation_timestamps) >= self.MAX_MUTATIONS_PER_HOUR:
            raise GuardrailViolation(
                f"Mutation rate limit: {len(self._mutation_timestamps)}"
                f"/{self.MAX_MUTATIONS_PER_HOUR} per hour"
            )
        self._mutation_timestamps.append(now)

    def status(self) -> dict:
        """Return guardrail status."""
        now = time.time()
        recent = [t for t in self._mutation_timestamps if now - t < 3600]
        return {
            "mutations_this_hour": len(recent),
            "max_per_hour": self.MAX_MUTATIONS_PER_HOUR,
            "violations_total": self._violations,
            "protected_modules": list(self.PROTECTED_MODULES),
            "protected_drives": list(self.PROTECTED_DRIVES),
        }


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@dataclass
class MutationRecord:
    """A single self-modification event."""
    timestamp: float
    target: str
    before: Any
    after: Any
    reason: str
    applied: bool = True
    clamped: bool = False
    source: str = "evolution"


class Audit:
    """Logs every mutation with timestamp, reason, before/after values."""

    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        # Load existing audit log from state
        if self._state.get("evolution.audit_log") is None:
            self._state.set("evolution.audit_log", [])
            self._state.set("evolution.audit_count", 0)

    def record(self, mutation: MutationRecord) -> None:
        """Record a mutation to the audit log."""
        entry = {
            "ts": mutation.timestamp,
            "target": mutation.target,
            "before": self._serialize(mutation.before),
            "after": self._serialize(mutation.after),
            "reason": mutation.reason,
            "applied": mutation.applied,
            "clamped": mutation.clamped,
            "source": mutation.source,
        }

        # Add hash for tamper detection
        chain_str = json.dumps(entry, sort_keys=True, default=str)
        entry["hash"] = hashlib.sha256(chain_str.encode()).hexdigest()[:16]

        log = list(self._state.get("evolution.audit_log") or [])
        log.append(entry)
        # Keep last 200 entries
        self._state.set("evolution.audit_log", log[-200:])
        count = int(self._state.get("evolution.audit_count") or 0) + 1
        self._state.set("evolution.audit_count", count)

        logger.info(
            "MUTATION #%d: %s: %s → %s (%s) reason: %s",
            count, mutation.target,
            self._serialize(mutation.before),
            self._serialize(mutation.after),
            "applied" if mutation.applied else "blocked",
            mutation.reason,
        )

    def recent(self, n: int = 10) -> List[dict]:
        """Get the N most recent mutations."""
        log = list(self._state.get("evolution.audit_log") or [])
        return log[-n:]

    def status(self) -> dict:
        """Audit summary."""
        log = list(self._state.get("evolution.audit_log") or [])
        count = int(self._state.get("evolution.audit_count") or 0)
        applied = sum(1 for e in log if e.get("applied", True))
        blocked = sum(1 for e in log if not e.get("applied", True))
        return {
            "total_mutations": count,
            "recent_applied": applied,
            "recent_blocked": blocked,
            "recent": log[-5:],
        }

    @staticmethod
    def _serialize(value: Any) -> Any:
        """Make a value JSON-serializable."""
        if isinstance(value, (str, int, float, bool, type(None))):
            return value
        try:
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            return str(value)


# ---------------------------------------------------------------------------
# Mutator
# ---------------------------------------------------------------------------

class Mutator:
    """Applies approved mutations to drive weights, cycle intervals, behavior flags."""

    def __init__(self, state: "StateEngine") -> None:
        self._state = state

    def propose(self, target: str, new_value: Any, reason: str = "") -> dict:
        """Create a mutation proposal."""
        current_value = self._state.get(target)
        return {
            "target": target,
            "current": current_value,
            "proposed": new_value,
            "reason": reason,
        }

    def apply(self, target: str, new_value: Any) -> Any:
        """Apply a mutation to state. Returns the old value."""
        old_value = self._state.get(target)
        self._state.set(target, new_value)
        return old_value


# ---------------------------------------------------------------------------
# Evolution (unified interface)
# ---------------------------------------------------------------------------

class Evolution:
    """Self-modification system for HypostasRuntime.

    Wraps Mutator + Guardrails + Audit into a single interface.
    Reads from PLASTICITY's performance data to propose weight adjustments.
    """

    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        self.mutator = Mutator(state)
        self.guardrails = Guardrails()
        self.audit = Audit(state)

    def propose_mutation(self, target: str, new_value: Any, reason: str) -> bool:
        """Propose + validate + apply a mutation. Returns True if applied."""
        proposal = self.mutator.propose(target, new_value, reason)
        current_value = proposal["current"]

        try:
            self.guardrails.validate(target, new_value, current_value)
        except GuardrailViolation as exc:
            logger.warning("Evolution: mutation blocked — %s", exc)
            self.audit.record(MutationRecord(
                timestamp=time.time(),
                target=target,
                before=current_value,
                after=new_value,
                reason=reason,
                applied=False,
            ))
            return False

        # Apply the mutation
        old_value = self.mutator.apply(target, new_value)
        self.audit.record(MutationRecord(
            timestamp=time.time(),
            target=target,
            before=old_value,
            after=new_value,
            reason=reason,
            applied=True,
        ))
        return True

    def status(self) -> dict:
        """Recent mutations, pending proposals, guardrail stats."""
        return {
            "guardrails": self.guardrails.status(),
            "audit": self.audit.status(),
        }

    def tick(self) -> None:
        """Check for PLASTICITY signals and auto-propose mutations.

        Reads plasticity history and proposes weight adjustments for
        drives that consistently produce good or bad outcomes.
        """
        try:
            plasticity_data = self._state.get("plasticity.history") or {}
            if not plasticity_data:
                return

            for drive_name, records in plasticity_data.items():
                if not isinstance(records, list) or len(records) < 5:
                    continue

                # Calculate success rate from recent records
                recent = records[-20:]
                successes = sum(1 for r in recent if r.get("success", False))
                success_rate = successes / len(recent)
                avg_quality = sum(r.get("quality", 0.5) for r in recent) / len(recent)

                # Composite score
                composite = 0.4 * success_rate + 0.3 * avg_quality + 0.3 * success_rate

                # Only propose if clearly good or bad
                if composite > 0.7:
                    # Drive is doing well — slight weight increase
                    current = self._state.get(f"drives.{drive_name}") or 0.5
                    if isinstance(current, (int, float)):
                        new_weight = min(2.0, current + 0.05)
                        if new_weight != current:
                            self.propose_mutation(
                                f"drives.{drive_name}",
                                round(new_weight, 4),
                                f"PLASTICITY: drive '{drive_name}' success rate "
                                f"{success_rate:.2f}, quality {avg_quality:.2f} — increasing weight",
                            )
                elif composite < 0.3:
                    # Drive is doing poorly — slight weight decrease
                    current = self._state.get(f"drives.{drive_name}") or 0.5
                    if isinstance(current, (int, float)):
                        new_weight = max(0.1, current - 0.05)
                        if new_weight != current:
                            self.propose_mutation(
                                f"drives.{drive_name}",
                                round(new_weight, 4),
                                f"PLASTICITY: drive '{drive_name}' success rate "
                                f"{success_rate:.2f}, quality {avg_quality:.2f} — decreasing weight",
                            )
        except Exception as exc:
            logger.debug("Evolution tick error: %s", exc)
