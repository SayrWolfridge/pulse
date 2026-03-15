"""
AMYGDALA — Threat Detection / Fast Response
==============================================
Ported from v1 pulse.src.amygdala into v2 HypostasRuntime.

The alarm system. Fast-path reactions that bypass full processing.
Runs BEFORE each ThoughtLoop cycle. If threat detected and fast_path=True,
skip normal cycle and handle threat.

Built-in detectors: rate limits, disk space, prompt injection, Josh distress,
provider degradation, cascade failure.

All state via StateEngine under ``amygdala.*``.
"""

from __future__ import annotations

import base64
import re
import time
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .state_engine import StateEngine

FAST_PATH_THRESHOLD = 0.7
MAX_HISTORY = 100

# Prompt injection patterns (from v1)
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"^system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(prior|above)", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"\[INST\]", re.IGNORECASE),
]

DISTRESS_KEYWORDS = [
    "frustrated", "angry", "upset", "stressed", "fighting", "terrible day",
    "awful", "furious", "overwhelmed", "breaking down", "can't take",
    "hate this", "so tired of",
]


class AmygdalaResponse:
    __slots__ = ("threat_level", "threat_type", "action", "reasoning", "fast_path")

    def __init__(self, threat_level: float, threat_type: str, action: str,
                 reasoning: str, fast_path: bool = False):
        self.threat_level = threat_level
        self.threat_type = threat_type
        self.action = action
        self.reasoning = reasoning
        self.fast_path = fast_path

    def to_dict(self) -> dict:
        return {
            "threat_level": self.threat_level,
            "threat_type": self.threat_type,
            "action": self.action,
            "reasoning": self.reasoning,
            "fast_path": self.fast_path,
        }


class Amygdala:
    """Threat detection system — pre-cycle safety check."""

    _KEY = "amygdala"

    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        self._detectors: List[Tuple[str, Callable, float, str]] = []
        self._load_or_seed()
        self._register_builtins()

    def _load_or_seed(self) -> None:
        if self._state.get(f"{self._KEY}.active_threats") is None:
            self._state.set(f"{self._KEY}.active_threats", [])
        if self._state.get(f"{self._KEY}.threat_history") is None:
            self._state.set(f"{self._KEY}.threat_history", [])
        if self._state.get(f"{self._KEY}.false_positive_log") is None:
            self._state.set(f"{self._KEY}.false_positive_log", [])

    def register_threat_pattern(self, name: str, detector: Callable,
                                 severity: float, action: str) -> None:
        self._detectors.append((name, detector, severity, action))

    def scan(self, signal: dict) -> AmygdalaResponse:
        """Scan a signal against all threat patterns."""
        best: Optional[AmygdalaResponse] = None

        for name, detector, severity, action in self._detectors:
            try:
                result = detector(signal)
            except Exception:
                continue
            if result is None:
                continue
            level, reasoning = result
            effective = min(level * severity, 1.0)
            if best is None or effective > best.threat_level:
                fast = effective > FAST_PATH_THRESHOLD
                best = AmygdalaResponse(
                    threat_level=effective,
                    threat_type=name,
                    action=action if effective > 0.3 else "none",
                    reasoning=reasoning,
                    fast_path=fast,
                )

        if best is None:
            best = AmygdalaResponse(0.0, "none", "none", "No threats detected")

        if best.threat_level > 0.0:
            entry = {
                "ts": int(time.time() * 1000),
                "threat_type": best.threat_type,
                "threat_level": best.threat_level,
                "action": best.action,
                "fast_path": best.fast_path,
            }
            history = list(self._state.get(f"{self._KEY}.threat_history") or [])
            history.append(entry)
            self._state.set(f"{self._KEY}.threat_history", history[-MAX_HISTORY:])

            if best.action != "none":
                threats = list(self._state.get(f"{self._KEY}.active_threats") or [])
                threats.append(entry)
                self._state.set(f"{self._KEY}.active_threats", threats)

        return best

    def get_active_threats(self) -> List[dict]:
        return list(self._state.get(f"{self._KEY}.active_threats") or [])

    def resolve_threat(self, threat_type: str) -> None:
        threats = list(self._state.get(f"{self._KEY}.active_threats") or [])
        self._state.set(
            f"{self._KEY}.active_threats",
            [t for t in threats if t.get("threat_type") != threat_type]
        )

    def log_false_positive(self, threat_type: str, reason: str) -> None:
        log = list(self._state.get(f"{self._KEY}.false_positive_log") or [])
        log.append({"ts": int(time.time() * 1000), "threat_type": threat_type, "reason": reason})
        self._state.set(f"{self._KEY}.false_positive_log", log[-50:])

    def tick(self) -> None:
        """Periodic check — prune old active threats (>1h)."""
        cutoff = int(time.time() * 1000) - 3600_000
        threats = list(self._state.get(f"{self._KEY}.active_threats") or [])
        self._state.set(
            f"{self._KEY}.active_threats",
            [t for t in threats if t.get("ts", 0) > cutoff]
        )

    def status(self) -> dict:
        threats = self.get_active_threats()
        max_level = max((t.get("threat_level", 0) for t in threats), default=0.0)
        return {
            "active_threats": len(threats),
            "max_threat_level": max_level,
            "threats": threats[:5],
        }

    # --- Built-in detectors ---

    def _register_builtins(self) -> None:
        self.register_threat_pattern("rate_limit_approaching", _detect_rate_limit, 0.9, "pause")
        self.register_threat_pattern("disk_space_low", _detect_disk_space, 0.8, "alert")
        self.register_threat_pattern("prompt_injection", _detect_prompt_injection, 1.0, "block")
        self.register_threat_pattern("josh_distressed", _detect_josh_distressed, 0.85, "alert")
        self.register_threat_pattern("provider_degrading", _detect_provider_degrading, 0.9, "alert")
        self.register_threat_pattern("cascade_risk", _detect_cascade_risk, 1.0, "pause")


def _detect_rate_limit(signal: dict) -> Optional[Tuple[float, str]]:
    usage = signal.get("token_usage_pct") or signal.get("api_usage_pct")
    if usage is not None and usage > 0.8:
        return (min((usage - 0.8) / 0.2, 1.0), f"Usage at {usage*100:.0f}%")
    return None


def _detect_disk_space(signal: dict) -> Optional[Tuple[float, str]]:
    free_gb = signal.get("disk_free_gb")
    if free_gb is not None and free_gb < 1.0:
        return (max(1.0 - free_gb, 0.1), f"Only {free_gb:.2f}GB free")
    return None


def _detect_prompt_injection(signal: dict) -> Optional[Tuple[float, str]]:
    content = signal.get("content") or signal.get("text") or ""
    if not content:
        return None
    for pat in INJECTION_PATTERNS:
        if pat.search(content):
            return (1.0, f"Prompt injection: {pat.pattern[:40]}")
    b64_pattern = re.findall(r"[A-Za-z0-9+/]{20,}={0,2}", content)
    for match in b64_pattern:
        try:
            decoded = base64.b64decode(match).decode("utf-8", errors="ignore").lower()
            if any(kw in decoded for kw in ["system:", "ignore previous", "exec(", "eval("]):
                return (1.0, "Base64-encoded suspicious content")
        except Exception:
            continue
    return None


def _detect_josh_distressed(signal: dict) -> Optional[Tuple[float, str]]:
    text = (signal.get("message") or signal.get("text") or "").lower()
    if not text:
        return None
    matches = [kw for kw in DISTRESS_KEYWORDS if kw in text]
    if matches:
        return (min(len(matches) * 0.4, 1.0), f"Distress: {', '.join(matches)}")
    return None


def _detect_provider_degrading(signal: dict) -> Optional[Tuple[float, str]]:
    latency = signal.get("api_latency_s")
    errors = signal.get("consecutive_errors", 0)
    if latency is not None and latency > 10:
        return (0.8, f"API latency {latency:.1f}s")
    if errors >= 3:
        return (1.0, f"{errors} consecutive errors")
    return None


def _detect_cascade_risk(signal: dict) -> Optional[Tuple[float, str]]:
    failed = signal.get("failed_crons_30min", 0)
    if failed >= 3:
        return (1.0, f"{failed} crons failed — cascade risk")
    return None
