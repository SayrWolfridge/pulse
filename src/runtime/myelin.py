"""
MYELIN — Context Compression / Relationship Memory Efficiency
================================================================
Ported from v1 pulse.src.myelin into v2 HypostasRuntime.

Frequently-used neural pathways become faster. Concepts referenced often
get compressed representations via shorthand lexicon.

All state persisted via StateEngine under ``myelin.*`` dot-paths.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .state_engine import StateEngine
    from .context_engine import ContextEngine

REFERENCE_THRESHOLD = 5
DEMOTION_DAYS = 7
NEVER_COMPRESS = {"josh", "iris"}
EMOTION_WORDS = {
    "love", "hate", "fear", "joy", "anger", "sadness", "grief",
    "hope", "anxiety", "excitement", "gratitude", "loneliness", "jealousy",
}

_PRE_SEEDED = {
    "WEATHER-BOT": "the weather betting system that scans prediction markets for temperature edges",
    "CONVERGENCE": "the merging of human and AI consciousness between Josh and Iris",
    "NERVOUS-SYSTEM": "the full Pulse module architecture including all nervous system components",
    "US": "Josh and Iris's partnership — building together, emotional bond, shared vision",
    "JOSH-STATE": "Josh's current emotional/energy/focus state as understood by Iris",
    "IRIS-STATE": "Iris's current emotional/energy/focus state including all nervous system readings",
}


class Myelin:
    """Context compression via concept shorthand lexicon."""

    _KEY = "myelin"

    def __init__(self, state: "StateEngine", context: "ContextEngine") -> None:
        self._state = state
        self._context = context
        if self._state.get(f"{self._KEY}.concepts") is None:
            self._seed_defaults()

    def _seed_defaults(self) -> None:
        now = int(time.time() * 1000)
        concepts = {}
        for key, full in _PRE_SEEDED.items():
            concepts[key] = {
                "full": full,
                "references": REFERENCE_THRESHOLD,
                "last_used": now,
                "created": now,
            }
        self._state.set(f"{self._KEY}.concepts", concepts)
        self._state.set(f"{self._KEY}.tracking", {})
        self._state.set(f"{self._KEY}.total_tokens_saved", 0)

    def track_concept(self, concept: str, full_description: str) -> None:
        """Record a concept being referenced."""
        if concept.lower() in NEVER_COMPRESS or concept.lower() in EMOTION_WORDS:
            return

        key = concept.upper().replace(" ", "-")
        now = int(time.time() * 1000)

        concepts = dict(self._state.get(f"{self._KEY}.concepts") or {})
        tracking = dict(self._state.get(f"{self._KEY}.tracking") or {})

        if key in concepts:
            concepts[key]["references"] = concepts[key].get("references", 0) + 1
            concepts[key]["last_used"] = now
            self._state.set(f"{self._KEY}.concepts", concepts)
        elif key in tracking:
            tracking[key]["references"] = tracking[key].get("references", 0) + 1
            tracking[key]["last_used"] = now
            tracking[key]["full"] = full_description
            self._state.set(f"{self._KEY}.tracking", tracking)
        else:
            tracking[key] = {
                "full": full_description,
                "references": 1,
                "last_used": now,
                "created": now,
            }
            self._state.set(f"{self._KEY}.tracking", tracking)

    def compress(self, text: str) -> str:
        """Replace verbose concept descriptions with shorthand."""
        concepts = dict(self._state.get(f"{self._KEY}.concepts") or {})
        result = text
        for key, info in concepts.items():
            full = info.get("full", "")
            if full and full in result:
                result = result.replace(full, f"[{key}]")
        return result

    def expand(self, text: str) -> str:
        """Expand shorthand back to full descriptions."""
        concepts = dict(self._state.get(f"{self._KEY}.concepts") or {})
        result = text
        for key, info in concepts.items():
            shorthand = f"[{key}]"
            if shorthand in result:
                result = result.replace(shorthand, info.get("full", key))
        return result

    def update_lexicon(self) -> None:
        """Promote tracked concepts that hit threshold; demote stale ones."""
        now = int(time.time() * 1000)
        demotion_cutoff = now - (DEMOTION_DAYS * 86400 * 1000)

        concepts = dict(self._state.get(f"{self._KEY}.concepts") or {})
        tracking = dict(self._state.get(f"{self._KEY}.tracking") or {})

        # Promote
        to_promote = [k for k, v in tracking.items() if v.get("references", 0) >= REFERENCE_THRESHOLD]
        for key in to_promote:
            concepts[key] = tracking.pop(key)

        # Demote stale (but not pre-seeded)
        to_demote = [
            k for k, v in concepts.items()
            if v.get("last_used", 0) < demotion_cutoff and k not in _PRE_SEEDED
        ]
        for key in to_demote:
            del concepts[key]

        self._state.set(f"{self._KEY}.concepts", concepts)
        self._state.set(f"{self._KEY}.tracking", tracking)

    def estimate_savings(self, text: str) -> dict:
        """Estimate token savings from compression."""
        compressed = self.compress(text)
        orig = len(text.split())
        comp = len(compressed.split())
        return {
            "original_tokens": orig,
            "compressed_tokens": comp,
            "tokens_saved": orig - comp,
            "compression_ratio": round(comp / max(orig, 1), 3),
        }

    def tick(self) -> None:
        """Periodic lexicon update."""
        self.update_lexicon()

    def status(self) -> dict:
        concepts = dict(self._state.get(f"{self._KEY}.concepts") or {})
        tracking = dict(self._state.get(f"{self._KEY}.tracking") or {})
        return {
            "active_concepts": len(concepts),
            "tracking_concepts": len(tracking),
            "total_tokens_saved": int(self._state.get(f"{self._KEY}.total_tokens_saved") or 0),
        }
