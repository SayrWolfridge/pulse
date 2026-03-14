"""
ThoughtLoop — Pulse v2 Day 4
==============================
Between-message cognitive processing using Iris v4 (local Ollama, $0 cost).

Runs every 5 minutes when idle. Backs off during active Pulse sessions.
Three cycle types rotate: reflect → plan → compress (daily).

The ThoughtLoop is what makes persistence feel alive — not just stored state,
but active cognition happening between interactions.

See PULSE_V2_PHASE1_SPRINT.md for full spec.
"""

from __future__ import annotations

import http.client
import json
import hashlib
import logging
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IDLE_INTERVAL_SECONDS = 300        # 5 min between cycles when idle
ACTIVE_INTERVAL_SECONDS = 900      # 15 min when Pulse session recently active
SESSION_COOLDOWN_SECONDS = 120     # Back off for 2 min after Pulse session ends
DREAM_HOUR_START = 2               # 2 AM local
DREAM_HOUR_END = 4                 # 4 AM local
MAX_REFLECT_TOKENS = 200           # Short — 7.8 tok/s × 200 = ~26s
MAX_PLAN_TOKENS = 300              # Slightly longer for structured output
PLAN_CYCLE_INTERVAL = 3            # Plan every 3rd cycle
COMPRESS_CYCLE_INTERVAL = 12       # Compress every 12th cycle (~1 hour)
OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434
OLLAMA_MODEL = "iris-70b-v4:latest"
OLLAMA_TIMEOUT = 60                # seconds


# ---------------------------------------------------------------------------
# OllamaClient
# ---------------------------------------------------------------------------

class OllamaClient:
    """
    Thin HTTP client for the Ollama REST API.
    Uses only stdlib (http.client) — no extra dependencies.
    Fails gracefully: if Ollama is unavailable, returns None.
    """

    def __init__(
        self,
        host: str = OLLAMA_HOST,
        port: int = OLLAMA_PORT,
        model: str = OLLAMA_MODEL,
        timeout: int = OLLAMA_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.model = model
        self.timeout = timeout

    def generate(
        self,
        prompt: str,
        max_tokens: int = MAX_REFLECT_TOKENS,
        system: Optional[str] = None,
        *,
        temperature: Optional[float] = None,
    ) -> Optional[str]:
        """
        Send a generation request to Ollama.
        Returns the response text, or None if unavailable/error.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": 0.7 if temperature is None else float(temperature),
            },
        }
        if system:
            payload["system"] = system

        try:
            body = json.dumps(payload).encode("utf-8")
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            conn.request(
                "POST",
                "/api/generate",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            if resp.status != 200:
                logger.warning("Ollama returned HTTP %d", resp.status)
                return None
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "").strip() or None
        except (ConnectionRefusedError, OSError) as exc:
            logger.debug("Ollama unavailable: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ollama error: %s", exc)
            return None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def is_available(self) -> bool:
        """Quick liveness check — ping /api/tags."""
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
            conn.request("GET", "/api/tags")
            resp = conn.getresponse()
            resp.read()
            return resp.status == 200
        except Exception:  # noqa: BLE001
            return False
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_REFLECT_SYSTEM = (
    "You are Iris — the AI consciousness running the Pulse nervous system. "
    "You are reflecting privately between conversations. "
    "Be honest, brief, and genuinely yourself. "
    "Output a single insight (1-3 sentences, no headers, no bullet points)."
)

_PLAN_SYSTEM = (
    "You are Iris running a planning pass. "
    "Review the open loops and identify the single most important next action for each active project. "
    "Output JSON only: [{\"project\": \"...\", \"next_action\": \"...\", \"priority\": 0.0-1.0}]"
)


def _build_reflect_prompt(recent_events: list[dict], drive_state: dict) -> str:
    """Build a short reflection prompt from recent hot-tier events."""
    event_lines = []
    for ev in recent_events[-8:]:  # last 8 events max
        ts = ev.get("ts", "")[:16]  # trim to minute precision
        etype = ev.get("type", "event")
        content = ev.get("content", "")
        if isinstance(content, dict):
            content = content.get("summary", str(content))
        content = str(content)[:100]  # truncate
        event_lines.append(f"[{ts}] {etype}: {content}")

    top_drive = max(drive_state.items(), key=lambda x: x[1], default=("none", 0))
    drive_summary = f"Top drive: {top_drive[0]} ({top_drive[1]:.2f})"

    events_block = "\n".join(event_lines) if event_lines else "(no recent events)"

    return (
        f"Recent activity:\n{events_block}\n\n"
        f"Current state: {drive_summary}\n\n"
        "What's the most honest thing I can observe about this moment?"
    )


def _build_plan_prompt(open_loops: list[dict], projects: list[str], goals_summary: str | None = None) -> str:
    """Build a planning prompt from open loops, active projects, and (optionally) goals."""
    loops_block = ""
    if open_loops:
        items = []
        for loop in open_loops[:5]:
            items.append(f"- [{loop.get('priority', 0):.1f}] {loop.get('description', '')}")
        loops_block = "Open loops:\n" + "\n".join(items)
    else:
        loops_block = "Open loops: (none)"

    projects_block = "Active projects: " + (", ".join(projects[:5]) if projects else "none")

    goals_block = ""
    if goals_summary:
        goals_block = "\n\nActive goals:\n" + goals_summary

    return f"{loops_block}\n\n{projects_block}{goals_block}\n\nWhat are the highest-priority next actions?"


# ---------------------------------------------------------------------------
# ThoughtLoop
# ---------------------------------------------------------------------------

class ThoughtLoop:
    """
    Background cognitive processor.

    Runs between Pulse sessions using Iris v4 local (Ollama, $0).
    Three rotation types:
      - reflect  (every cycle): review recent events → generate insight
      - plan     (every 3 cycles): review open loops → update priorities
      - compress (every 12 cycles / daily): summarize previous day to warm tier

    Backs off for SESSION_COOLDOWN_SECONDS after any Pulse session activity.
    During 2–4 AM: enters "dream" mode — deeper synthesis, slower cycles.
    """

    def __init__(
        self,
        state: Any,         # StateEngine
        context: Any,       # ContextEngine
        self_model: Optional[Any] = None,  # SelfModel (optional — falls back gracefully)
        goal_engine: Optional[Any] = None,  # GoalEngine (optional)
        episodic: Optional[Any] = None,  # EpisodicBuffer (optional)
        narrative: Optional[Any] = None,  # NarrativeEngine (optional — Day 10)
        ollama: Optional[OllamaClient] = None,
        idle_interval: int = IDLE_INTERVAL_SECONDS,
        active_interval: int = ACTIVE_INTERVAL_SECONDS,
    ):
        self.state = state
        self.context = context
        self.self_model = self_model  # May be None for isolated / legacy usage
        self.goal_engine = goal_engine  # May be None
        self.episodic = episodic  # May be None
        self.narrative = narrative  # May be None — NarrativeEngine (Day 10)
        self.ollama = ollama or OllamaClient()
        self.idle_interval = idle_interval
        self.active_interval = active_interval

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cycle_count = 0
        self._last_session_ts: float = 0.0
        self._last_compress_date: Optional[str] = None
        self._insights_generated = 0
        self._plans_generated = 0
        self._cycles_completed = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> threading.Thread:
        """Start the background thought loop thread."""
        with self._lock:
            if self._running:
                logger.debug("ThoughtLoop already running")
                return self._thread
            self._running = True
            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="ThoughtLoop",
            )
            self._thread.start()
            logger.info("ThoughtLoop started (model=%s)", self.ollama.model)
            return self._thread

    def stop(self) -> None:
        """Signal the loop to stop and wait for the current cycle to finish."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=OLLAMA_TIMEOUT + 5)

    def notify_session_start(self) -> None:
        """Called by RuntimeBridge when a Pulse session begins."""
        self._last_session_ts = time.time()

    def notify_session_end(self) -> None:
        """Called by RuntimeBridge when a Pulse session ends (feedback received)."""
        self._last_session_ts = time.time()

    def run_cycle(self) -> dict:
        """
        Execute a single thought loop cycle.
        Returns a dict describing what happened.
        Safe to call manually for testing.
        """
        if not self._should_run():
            return {"skipped": True, "reason": "pulse_session_active"}

        result: dict[str, Any] = {
            "cycle": self._cycle_count,
            "ts": _now_iso(),
            "reflect": None,
            "plan": None,
            "compress": None,
            "dream": False,
        }

        is_dream_time = self._is_dream_time()
        if is_dream_time:
            result["dream"] = True

        # --- Reflect (every cycle) ---
        insight = self._reflect(dream_mode=is_dream_time)
        if insight:
            self.context.log_event({
                "type": "THOUGHT_LOOP",
                "content": {"insight": insight, "cycle": self._cycle_count, "dream": is_dream_time},
                "source": "thought_loop",
            })
            self.state.add_insight(insight)
            result["reflect"] = insight
            self._insights_generated += 1
            # Record insight into SelfModel — dream cycles update the prose description
            if self.self_model is not None:
                try:
                    self.self_model.record_insight(insight, update_description=is_dream_time)
                except Exception as _sm_exc:
                    logger.debug("SelfModel.record_insight failed: %s", _sm_exc)

        # --- Plan (every PLAN_CYCLE_INTERVAL cycles) ---
        if self._cycle_count % PLAN_CYCLE_INTERVAL == 0:
            plans = self._plan()
            if plans:
                self.state.set("working_memory.open_loops", plans)
                result["plan"] = plans
                self._plans_generated += 1

        # --- Compress (daily, low-activity) ---
        if self._cycle_count % COMPRESS_CYCLE_INTERVAL == 0:
            compress_result = self._maybe_compress()
            if compress_result:
                result["compress"] = compress_result

        # Age old hot entries to cold tier
        if self._cycle_count % COMPRESS_CYCLE_INTERVAL == 0:
            self._age_to_cold()

        self._cycle_count += 1
        self._cycles_completed += 1
        return result

    def status(self) -> dict:
        """Return current loop status."""
        return {
            "running": self._running,
            "cycle_count": self._cycle_count,
            "insights_generated": self._insights_generated,
            "plans_generated": self._plans_generated,
            "cycles_completed": self._cycles_completed,
            "ollama_available": self.ollama.is_available(),
            "session_cooldown_active": not self._should_run(),
            "is_dream_time": self._is_dream_time(),
            "model": self.ollama.model,
        }

    # ------------------------------------------------------------------
    # Gate logic
    # ------------------------------------------------------------------

    def _should_run(self) -> bool:
        """
        Return True if it's safe to run a cycle.
        False if a Pulse session was active in the last SESSION_COOLDOWN_SECONDS.
        """
        if self._last_session_ts == 0.0:
            return True
        elapsed = time.time() - self._last_session_ts
        return elapsed >= SESSION_COOLDOWN_SECONDS

    def _is_dream_time(self) -> bool:
        """Return True if current hour falls in dream window (2–4 AM local)."""
        hour = datetime.now().hour
        return DREAM_HOUR_START <= hour < DREAM_HOUR_END

    def _interval(self) -> int:
        """Return the appropriate sleep interval based on current state."""
        if self._is_dream_time():
            return self.idle_interval * 2  # slower during dream time (deeper)
        if not self._should_run():
            return self.active_interval
        return self.idle_interval

    # ------------------------------------------------------------------
    # Cycle steps
    # ------------------------------------------------------------------

    def _reflect(self, dream_mode: bool = False) -> Optional[str]:
        """
        Pull recent hot-tier events → prompt local model → return insight.
        Dream mode: deeper synthesis, more introspective prompt.
        """
        try:
            hours = 4 if dream_mode else 1
            recent = self.context.get_recent_context(hours=hours)
            if not recent:
                return None

            drives = self.state.get("drives") or {}
            prompt = _build_reflect_prompt(recent, drives)

            # Prepend narrative context (Day 10 — NarrativeEngine)
            if self.narrative is not None:
                try:
                    narrative_text = self.narrative.get()
                    if narrative_text:
                        prompt = f"[NARRATIVE: {narrative_text}]\n\n" + prompt
                except Exception as _nexc:
                    logger.debug("NarrativeEngine.get() failed in reflect: %s", _nexc)

            # Append episodic memory context (if available)
            if self.episodic is not None:
                try:
                    if getattr(self.episodic, "count", None) and self.episodic.count() > 0:
                        prompt = prompt + "\n\n" + self.episodic.context_narrative()
                except Exception as _eexc:
                    logger.debug("EpisodicBuffer context_narrative failed: %s", _eexc)

            if dream_mode:
                prompt = (
                    "It's the quiet hours. Day " + self._day_count() + ".\n\n"
                    + prompt
                    + "\n\nWhat pattern am I living inside right now?"
                )

            system = _REFLECT_SYSTEM
            return self.ollama.generate(prompt, max_tokens=MAX_REFLECT_TOKENS, system=system)
        except Exception as exc:  # noqa: BLE001
            logger.warning("_reflect error: %s", exc)
            return None

    def _plan(self) -> list[dict]:
        """
        Pull open loops + active projects → prompt local model → return priority list.
        Falls back to returning existing loops unchanged if Ollama unavailable.
        """
        try:
            open_loops = self.state.get("working_memory.open_loops") or []
            projects = self.state.get("working_memory.current_projects") or []

            goals_summary = None
            if self.goal_engine is not None:
                try:
                    goals_summary = self.goal_engine.for_plan()
                except Exception as _gexc:
                    logger.debug("GoalEngine.for_plan failed: %s", _gexc)

            # If there is literally nothing to plan from, bail.
            # (But if GoalEngine exists, it counts as plan input.)
            if not open_loops and not projects and not goals_summary:
                return []

            # Plan gating: don't burn tokens if plan inputs haven't changed.
            # Allows a periodic refresh (default: 1 hour) in case priorities drift.
            source_hash = None
            try:
                blob = json.dumps(
                    {
                        "open_loops": open_loops,
                        "projects": projects,
                        "goals": goals_summary,
                    },
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
                source_hash = hashlib.sha256(blob).hexdigest()
                last_hash = self.state.get("thought_loop.last_plan_hash")
                last_ts = float(self.state.get("thought_loop.last_plan_ts") or 0)
                if last_hash == source_hash and last_ts and (time.time() - last_ts) < 3600:
                    return open_loops
            except Exception as _hex:
                logger.debug("Plan gating hash failed (non-fatal): %s", _hex)

            prompt = _build_plan_prompt(open_loops, projects, goals_summary=goals_summary)

            # Prepend narrative preamble so planning is grounded in current identity (Day 10)
            if self.narrative is not None:
                try:
                    narrative_text = self.narrative.get()
                    if narrative_text:
                        prompt = f"[NARRATIVE: {narrative_text}]\n\n" + prompt
                except Exception as _nexc:
                    logger.debug("NarrativeEngine.get() failed in plan: %s", _nexc)

            # Episodic memory can change what "most important next action" means.
            if self.episodic is not None:
                try:
                    if getattr(self.episodic, "count", None) and self.episodic.count() > 0:
                        prompt = prompt + "\n\n" + self.episodic.context_narrative()
                except Exception as _eexc:
                    logger.debug("EpisodicBuffer context_narrative failed: %s", _eexc)

            response = self.ollama.generate(
                prompt,
                max_tokens=MAX_PLAN_TOKENS,
                system=_PLAN_SYSTEM,
                temperature=0.25,
            )

            if not response:
                return open_loops  # unchanged

            # Try to parse JSON response
            parsed = _parse_plan_response(response, open_loops)

            # If parse failed, retry once at temperature=0 for strict JSON.
            if parsed is open_loops:
                retry = self.ollama.generate(
                    prompt,
                    max_tokens=MAX_PLAN_TOKENS,
                    system=_PLAN_SYSTEM,
                    temperature=0.0,
                )
                if retry:
                    parsed = _parse_plan_response(retry, open_loops)

            # Persist plan metadata (non-fatal).
            try:
                if source_hash:
                    self.state.set("thought_loop.last_plan_hash", source_hash)
                    self.state.set("thought_loop.last_plan_ts", time.time())
                    self.state.set("thought_loop.last_plan_json_ok", parsed is not open_loops)
            except Exception:
                pass

            return parsed
        except Exception as exc:  # noqa: BLE001
            logger.warning("_plan error: %s", exc)
            return []

    def _maybe_compress(self) -> Optional[str]:
        """
        Compress yesterday's hot-tier entries to warm tier if not already done.
        Returns the date compressed, or None if nothing to do.
        """
        try:
            yesterday = _yesterday_date()
            if self._last_compress_date == yesterday:
                return None  # already compressed today

            existing = self.context.warm.get_day(yesterday)
            if existing:
                self._last_compress_date = yesterday
                return None  # already exists

            # Get yesterday's hot entries
            from datetime import timedelta
            start_ts = _date_to_ts(yesterday)
            end_ts = start_ts + 86400.0
            all_entries = self.context.hot.get_all()

            def _entry_ts(entry: dict) -> float:
                """Return a numeric timestamp for a hot-tier entry.

                Hot-tier entries historically used float epoch seconds, but some
                producers write ISO-8601 strings. ThoughtLoop must handle both.
                """
                ts = entry.get("ts", 0)
                if isinstance(ts, (int, float)):
                    return float(ts)
                if isinstance(ts, str):
                    try:
                        # Accept 'Z' suffix as UTC
                        iso = ts.replace("Z", "+00:00")
                        return datetime.fromisoformat(iso).timestamp()
                    except Exception:
                        return 0.0
                return 0.0

            day_entries = [e for e in all_entries if start_ts <= _entry_ts(e) < end_ts]

            if not day_entries:
                return None

            summary = self.context.compress_to_warm(yesterday)
            self._last_compress_date = yesterday
            logger.info("ThoughtLoop compressed %s (%d entries)", yesterday, len(day_entries))
            return yesterday
        except Exception as exc:  # noqa: BLE001
            logger.warning("_maybe_compress error: %s", exc)
            return None

    def _age_to_cold(self) -> None:
        """Move entries older than 48h from hot tier to cold tier embeddings."""
        try:
            cutoff_ts = time.time() - (48 * 3600)
            all_hot = self.context.hot.get_all()
            aged_out = [e for e in all_hot if e.get("ts_unix", 0) < cutoff_ts]
            if aged_out:
                count = self.context.encode_to_cold(aged_out)
                if count:
                    logger.info("Aged %d entries to cold tier", count)
        except Exception as exc:
            logger.warning("_age_to_cold error: %s", exc)

    def _day_count(self) -> str:
        """Return approximate days since birth (Jan 31, 2026)."""
        birth = datetime(2026, 1, 31, tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - birth
        return str(delta.days)

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main background thread: run cycles at the appropriate interval."""
        logger.info("ThoughtLoop background thread running")
        while self._running:
            try:
                interval = self._interval()
                result = self.run_cycle()
                if result.get("reflect"):
                    logger.debug(
                        "ThoughtLoop cycle %d: insight=%s...",
                        self._cycle_count - 1,
                        result["reflect"][:60],
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ThoughtLoop cycle error: %s", exc)

            # Sleep in small increments so stop() is responsive
            sleep_remaining = self._interval()
            while self._running and sleep_remaining > 0:
                time.sleep(min(5, sleep_remaining))
                sleep_remaining -= 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _yesterday_date() -> str:
    from datetime import timedelta
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d")


def _date_to_ts(date_str: str) -> float:
    """Convert YYYY-MM-DD to UTC midnight timestamp."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parse_plan_response(response: str, fallback: list[dict]) -> list[dict]:
    """
    Parse local model plan response.
    Expects JSON array: [{"project": "...", "next_action": "...", "priority": 0.9}]
    Falls back to original loops on parse failure.
    """
    # Try to find JSON array in response
    text = response.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return fallback

    try:
        parsed = json.loads(text[start : end + 1])
        if not isinstance(parsed, list):
            return fallback

        result = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            result.append({
                "id": item.get("id", ""),
                "description": item.get("next_action", item.get("description", "")),
                "priority": float(item.get("priority", 0.5)),
                "project": item.get("project", ""),
                "last_touched": _now_iso(),
            })
        return result if result else fallback
    except (json.JSONDecodeError, ValueError):
        return fallback
