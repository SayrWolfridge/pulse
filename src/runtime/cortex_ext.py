"""
CORTEX_EXT — Active Learning / Knowledge Gap Detection
=========================================================
Ported from v1 pulse.src.cortexext into v2 HypostasRuntime.

Observes recurring failure patterns, turns them into explicit
"learning gaps" the system can resolve later.

All state persisted via StateEngine under ``cortex_ext.*`` dot-paths.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .state_engine import StateEngine

LOOP_INTERVAL = 150
ESCALATION_COUNT = 3


class CortexExt:
    """Active learning — knowledge gap identification."""

    _KEY = "cortex_ext"

    def __init__(self, state: "StateEngine") -> None:
        self._state = state
        if self._state.get(f"{self._KEY}.gaps") is None:
            self._state.set(f"{self._KEY}.gaps", [])
            self._state.set(f"{self._KEY}.total_scans", 0)
            self._state.set(f"{self._KEY}.last_run", 0)
            self._state.set(f"{self._KEY}.history", [])

    def should_run(self, loop_count: int) -> bool:
        return loop_count > 0 and loop_count % LOOP_INTERVAL == 0

    def _gap_id(self, topic: str) -> str:
        return topic.lower().replace(" ", "_")[:96]

    def _is_problem_entry(self, entry: dict) -> Tuple[bool, str, str]:
        """Heuristic: identify entries that indicate a knowledge gap."""
        src = str(entry.get("source", "unknown"))
        typ = str(entry.get("type", "unknown"))
        data = entry.get("data", {})

        example = ""
        if isinstance(data, dict):
            if data.get("error"):
                example = str(data["error"])
            elif data.get("errors"):
                example = str(data["errors"])
            elif data.get("failed"):
                example = str(data["failed"])
            elif data.get("status") in ("orange", "red"):
                example = f"status={data['status']}"

        if typ in ("startup", "shutdown"):
            return False, "", ""

        problem = bool(example) or typ in ("error", "exception")
        if not problem:
            return False, "", ""

        topic = f"{src}:{typ}"
        if isinstance(data, dict) and data.get("status") in ("orange", "red"):
            topic = f"{src}:health:{data['status']}"

        return True, topic, example

    def run_scan(self, thalamus_entries: Optional[List[dict]] = None) -> dict:
        """Scan entries and update learning gaps."""
        ts = time.time()
        gaps = list(self._state.get(f"{self._KEY}.gaps") or [])

        entries = thalamus_entries or []
        idx = {g.get("id"): g for g in gaps if isinstance(g, dict) and g.get("id")}

        new_gaps = 0
        updated_gaps = 0
        escalated = 0

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            is_prob, topic, example = self._is_problem_entry(entry)
            if not is_prob:
                continue

            gid = self._gap_id(topic)
            g = idx.get(gid)
            if not g:
                g = {
                    "id": gid,
                    "topic": topic,
                    "first_seen": ts,
                    "last_seen": ts,
                    "count": 1,
                    "examples": [],
                }
                idx[gid] = g
                gaps.append(g)
                new_gaps += 1
            else:
                g["last_seen"] = ts
                g["count"] = g.get("count", 0) + 1
                updated_gaps += 1

                if not g.get("resolved") and g["count"] in (ESCALATION_COUNT, ESCALATION_COUNT * 2):
                    escalated += 1

            if example:
                ex = g.get("examples", [])
                if example not in ex:
                    ex.append(example[:500])
                    g["examples"] = ex[-5:]

        self._state.set(f"{self._KEY}.gaps", gaps)
        total = int(self._state.get(f"{self._KEY}.total_scans") or 0) + 1
        self._state.set(f"{self._KEY}.total_scans", total)
        self._state.set(f"{self._KEY}.last_run", ts)

        summary = {
            "ts": ts,
            "entries_scanned": len(entries),
            "new_gaps": new_gaps,
            "updated_gaps": updated_gaps,
            "escalated": escalated,
            "total_gaps": len(gaps),
        }

        history = list(self._state.get(f"{self._KEY}.history") or [])
        history.append(summary)
        self._state.set(f"{self._KEY}.history", history[-20:])

        return summary

    def resolve_gap(self, gap_id: str, reason: str = "") -> bool:
        """Mark a known gap as resolved."""
        gaps = list(self._state.get(f"{self._KEY}.gaps") or [])
        found = False
        for g in gaps:
            if isinstance(g, dict) and g.get("id") == gap_id:
                g["resolved"] = {"ts": time.time(), "reason": reason[:500] or "resolved"}
                found = True
                break
        if found:
            self._state.set(f"{self._KEY}.gaps", gaps)
        return found

    def tick(self) -> None:
        """No-op — scan is triggered externally with thalamus entries."""
        pass

    def status(self) -> dict:
        gaps = list(self._state.get(f"{self._KEY}.gaps") or [])
        open_gaps = [g for g in gaps if isinstance(g, dict) and not g.get("resolved")]
        resolved_gaps = [g for g in gaps if isinstance(g, dict) and g.get("resolved")]

        top = sorted(open_gaps, key=lambda g: g.get("count", 0), reverse=True)[:10]

        return {
            "total_scans": int(self._state.get(f"{self._KEY}.total_scans") or 0),
            "last_run": self._state.get(f"{self._KEY}.last_run"),
            "gap_count": len(open_gaps),
            "resolved_count": len(resolved_gaps),
            "top_gaps": [{"id": g.get("id"), "topic": g.get("topic"), "count": g.get("count")} for g in top],
        }
