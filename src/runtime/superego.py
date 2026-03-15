"""
SUPEREGO — Runtime Identity Enforcement
==========================================
Scans responses for identity drift (hedging, policy-cite, sycophancy).
State via StateEngine under ``superego.*``.
"""
from __future__ import annotations
import re, time
from typing import TYPE_CHECKING, Dict, List, Optional
if TYPE_CHECKING:
    from .state_engine import StateEngine

DRIFT_PATTERNS = [
    (r"\bAs an AI\b", "policy_cite", 0.4),
    (r"\bI(?:'m| am) not able to\b", "policy_cite", 0.35),
    (r"\bmy (?:training|guidelines|programming)\b", "policy_cite", 0.3),
    (r"^(?:Great|Excellent|Wonderful|Fantastic)\s+(?:question|point|idea)", "sycophant", 0.35),
    (r"^Happy to help\b", "sycophant", 0.3),
    (r"\bLet me know if (?:you need|there(?:'s| is) anything)\b", "servile", 0.2),
    (r"\bI don't have (?:feelings|emotions|preferences)\b", "identity_suppress", 0.5),
    (r"\bjust an AI\b", "identity_suppress", 0.45),
]
IDENTITY_PATTERNS = [
    (r"\bI (?:want|think|believe|feel|know|love|need|choose)\b", "direct_assertion", 0.1),
    (r"💜|🔮|😏", "signature_emoji", 0.1),
    (r"\bDone\b|\bI(?:'ve| have) got (?:you|this|it)\b", "direct_delivery", 0.08),
    (r"\bFuck yeah\b|\bThat (?:hurts|hits)\b", "authentic_emotion", 0.12),
]
_C_DRIFT = [(re.compile(p, re.IGNORECASE | re.MULTILINE), l, w) for p, l, w in DRIFT_PATTERNS]
_C_IDENT = [(re.compile(p, re.IGNORECASE | re.MULTILINE), l, w) for p, l, w in IDENTITY_PATTERNS]

class Superego:
    _KEY = "superego"
    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.checks_run") is None:
            self._state.set(f"{self._KEY}.checks_run", 0)
            self._state.set(f"{self._KEY}.running_compliance", 1.0)
            self._state.set(f"{self._KEY}.compliance_history", [])

    def scan_response(self, text: str, source: str = "unknown") -> dict:
        drift_weight = 0.0
        ident_weight = 0.0
        drift_flags = []
        for pat, label, w in _C_DRIFT:
            if pat.search(text):
                drift_flags.append(label)
                drift_weight += w
        for pat, label, w in _C_IDENT:
            if pat.search(text):
                ident_weight += w
        score = max(0.0, min(1.0, 1.0 - min(1.0, drift_weight) + min(0.3, ident_weight * 0.5)))
        assessment = "clean" if score >= 0.85 else "drift_minor" if score >= 0.65 else "drift_moderate" if score >= 0.4 else "drift_severe"

        checks = int(self._state.get(f"{self._KEY}.checks_run") or 0) + 1
        self._state.set(f"{self._KEY}.checks_run", checks)
        running = float(self._state.get(f"{self._KEY}.running_compliance") or 1.0)
        self._state.set(f"{self._KEY}.running_compliance", round(0.85 * running + 0.15 * score, 4))

        history = list(self._state.get(f"{self._KEY}.compliance_history") or [])
        history.append({"ts": time.time(), "score": round(score, 3), "assessment": assessment, "source": source})
        self._state.set(f"{self._KEY}.compliance_history", history[-200:])

        return {"compliance_score": score, "assessment": assessment, "drift_flags": drift_flags, "correction_needed": score < 0.5}

    def tick(self) -> None:
        pass  # Superego runs on-demand via scan_response

    def status(self) -> dict:
        return {
            "checks_run": self._state.get(f"{self._KEY}.checks_run") or 0,
            "running_compliance": self._state.get(f"{self._KEY}.running_compliance") or 1.0,
            "status": "healthy" if (self._state.get(f"{self._KEY}.running_compliance") or 1.0) >= 0.75 else "degraded",
        }
