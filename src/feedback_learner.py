"""
RL-lite Feedback Learner — Phase 4.

Adapts drive weights over time based on observed feedback outcomes.
Each feedback call is recorded in a rolling window per drive; an
Exponential Moving Average (EMA) of the outcome score is used to
compute a weight multiplier that shifts the drive's effective weight
up or down by at most MAX_ADJUSTMENT.

Outcome scoring
---------------
  success  →  +1.0
  partial  →  +0.3
  blocked  →   0.0
  failure  →  -1.0
  (unknown) →   0.0

Multiplier range: [1 - MAX_ADJUSTMENT, 1 + MAX_ADJUSTMENT]
  → currently [0.7, 1.3]

The multiplier is applied on top of the drive's configured weight via
DriveEngine.apply_learner_adjustments().  No weight is ever set below
MIN_WEIGHT_FLOOR (0.1) to prevent a drive from going completely silent.

State is persisted to ``<state_dir>/feedback_learner.json`` so
adjustments survive daemon restarts.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

# ── tuneable constants ─────────────────────────────────────────────────────────

WINDOW: int = 20         # rolling events kept per drive
ALPHA: float = 0.15      # EMA learning rate (higher = faster adaptation)
MAX_ADJUSTMENT: float = 0.30   # max ±30 % weight shift
MIN_WEIGHT_FLOOR: float = 0.10  # absolute minimum effective weight

OUTCOME_SCORES: Dict[str, float] = {
    "success": 1.0,
    "partial": 0.3,
    "blocked": 0.0,
    "failure": -1.0,
}

# ── data structures ────────────────────────────────────────────────────────────


class FeedbackEvent:
    """A single recorded feedback observation for one drive."""

    __slots__ = ("ts", "pressure", "outcome", "score")

    def __init__(self, pressure: float, outcome: str, ts: float | None = None):
        self.ts: float = ts if ts is not None else time.time()
        self.pressure: float = pressure
        self.outcome: str = outcome
        self.score: float = OUTCOME_SCORES.get(outcome, 0.0)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "pressure": self.pressure,
            "outcome": self.outcome,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FeedbackEvent":
        ev = cls.__new__(cls)
        ev.ts = float(d.get("ts", 0.0))
        ev.pressure = float(d.get("pressure", 0.0))
        ev.outcome = str(d.get("outcome", "unknown"))
        ev.score = float(d.get("score", OUTCOME_SCORES.get(ev.outcome, 0.0)))
        return ev


# ── learner ────────────────────────────────────────────────────────────────────


class FeedbackLearner:
    """
    RL-lite adaptive learner: adjusts drive weight multipliers based on
    rolling feedback outcome history.

    Usage::

        learner = FeedbackLearner("~/.pulse/state")
        learner.record("goals", pressure_at_trigger=2.3, outcome="success")
        adj = learner.get_weight_adjustment("goals")   # e.g. 1.08
    """

    def __init__(self, state_dir: str | Path):
        self._state_path = Path(state_dir).expanduser() / "feedback_learner.json"
        # drive_name → list of FeedbackEvent (capped at WINDOW)
        self._history: Dict[str, List[FeedbackEvent]] = {}
        # drive_name → EMA score in [-1.0, 1.0]
        self._ema: Dict[str, float] = {}
        self._load()

    # ── public API ─────────────────────────────────────────────────────────────

    def record(
        self,
        drive_name: str,
        pressure_at_trigger: float,
        outcome: str,
    ) -> float:
        """
        Record a feedback event and update the EMA for *drive_name*.

        Returns the new weight multiplier for convenience.
        """
        ev = FeedbackEvent(pressure=pressure_at_trigger, outcome=outcome)

        history = self._history.setdefault(drive_name, [])
        history.append(ev)
        if len(history) > WINDOW:
            history.pop(0)

        # EMA update: new_ema = alpha * score + (1 - alpha) * old_ema
        old_ema = self._ema.get(drive_name, 0.0)
        self._ema[drive_name] = ALPHA * ev.score + (1 - ALPHA) * old_ema

        self._save()
        multiplier = self.get_weight_adjustment(drive_name)
        logger.debug(
            "FeedbackLearner.record drive=%s outcome=%s ema=%.3f multiplier=%.3f",
            drive_name,
            outcome,
            self._ema[drive_name],
            multiplier,
        )
        return multiplier

    def get_weight_adjustment(self, drive_name: str) -> float:
        """
        Return the weight multiplier for *drive_name*.

        Range: [1 - MAX_ADJUSTMENT, 1 + MAX_ADJUSTMENT]  (default: 1.0)
        """
        ema = self._ema.get(drive_name, 0.0)
        # ema ∈ [-1, 1]; scale to [-MAX_ADJUSTMENT, +MAX_ADJUSTMENT]
        raw = 1.0 + ema * MAX_ADJUSTMENT
        # clamp to valid range
        return max(1.0 - MAX_ADJUSTMENT, min(1.0 + MAX_ADJUSTMENT, raw))

    def effective_weight(self, drive_name: str, base_weight: float) -> float:
        """
        Return the effective weight after applying the learner multiplier,
        clamped to MIN_WEIGHT_FLOOR.
        """
        adjusted = base_weight * self.get_weight_adjustment(drive_name)
        return max(MIN_WEIGHT_FLOOR, adjusted)

    def get_stats(self) -> dict:
        """
        Return a dict of learning statistics suitable for /status or /metrics.

        Schema::

            {
              "drives": {
                "<name>": {
                  "ema": float,
                  "multiplier": float,
                  "events": int,
                  "success_rate": float,   # 0–1
                  "last_outcome": str | null
                }
              },
              "total_events": int
            }
        """
        drives_stats: dict = {}
        total = 0
        for name, history in self._history.items():
            total += len(history)
            successes = sum(
                1 for ev in history if ev.outcome in ("success", "partial")
            )
            success_rate = successes / len(history) if history else 0.0
            drives_stats[name] = {
                "ema": round(self._ema.get(name, 0.0), 4),
                "multiplier": round(self.get_weight_adjustment(name), 4),
                "events": len(history),
                "success_rate": round(success_rate, 4),
                "last_outcome": history[-1].outcome if history else None,
            }
        return {"drives": drives_stats, "total_events": total}

    def reset_drive(self, drive_name: str) -> None:
        """Clear history and EMA for a drive (e.g. after config change)."""
        self._history.pop(drive_name, None)
        self._ema.pop(drive_name, None)
        self._save()

    # ── persistence ────────────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            data = {
                "version": 1,
                "ema": self._ema,
                "history": {
                    name: [ev.to_dict() for ev in evs]
                    for name, evs in self._history.items()
                },
            }
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._state_path)
        except OSError as exc:
            logger.warning("FeedbackLearner: could not save state: %s", exc)

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text())
            self._ema = {k: float(v) for k, v in data.get("ema", {}).items()}
            self._history = {
                name: [FeedbackEvent.from_dict(d) for d in evs]
                for name, evs in data.get("history", {}).items()
            }
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            logger.warning("FeedbackLearner: could not load state: %s", exc)
            self._history = {}
            self._ema = {}

    # ── prometheus helpers ─────────────────────────────────────────────────────

    def prometheus_lines(self) -> str:
        """
        Emit Prometheus text-format lines for learner metrics.

        Gauges::

            pulse_learner_ema{drive}         — EMA score [-1, 1]
            pulse_learner_multiplier{drive}  — weight multiplier [0.7, 1.3]
            pulse_learner_events{drive}      — events in rolling window
            pulse_learner_success_rate{drive}
        """
        lines: list[str] = []
        for name, stats in self.get_stats()["drives"].items():
            label = f'{{drive="{name}"}}'
            lines += [
                f"pulse_learner_ema{label} {stats['ema']}",
                f"pulse_learner_multiplier{label} {stats['multiplier']}",
                f"pulse_learner_events{label} {stats['events']}",
                f"pulse_learner_success_rate{label} {stats['success_rate']}",
            ]
        return "\n".join(lines)
