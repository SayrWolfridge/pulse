#!/usr/bin/env python3
"""
BIOSENSOR BRIDGE — Phase E1
Receives HealthKit data from iPhone Shortcuts and updates Pulse SOMA/ENDOCRINE state.

Architecture: Apple Watch → HealthKit → iPhone Shortcut → POST here → Pulse state update

Endpoints:
  POST /biosensor/heartrate    { "value": 72, "unit": "bpm" }
  POST /biosensor/hrv          { "value": 45, "unit": "ms" }
  POST /biosensor/activity     { "move": 400, "exercise": 20, "stand": 8, "goal_move": 600 }
  POST /biosensor/sleep        { "stage": "deep|core|rem|awake", "minutes": 90 }
  POST /biosensor/workout      { "type": "start|end", "activity": "running|strength|..." }
  GET  /biosensor/status       Returns current biometric state

Usage:
  python3 biosensor_bridge.py --port 9721 --host 0.0.0.0

Expose via Cloudflare tunnel (existing astra-trading tunnel):
  Add route: api.astra-hq.com/biosensor/* → localhost:9721/biosensor/*

Then iPhone Shortcut POSTs to: https://api.astra-hq.com/biosensor/heartrate
"""

import json
import time
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
import argparse

# ─── Paths ────────────────────────────────────────────────────────────────────

_DEFAULT_STATE_DIR = Path.home() / ".pulse" / "state"
_DEFAULT_BIOSENSOR_FILE = _DEFAULT_STATE_DIR / "biosensor-state.json"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("biosensor_bridge")

# ─── Biometric → Pulse State Mapping ─────────────────────────────────────────


def _load_biosensor_state() -> dict:
    if _DEFAULT_BIOSENSOR_FILE.exists():
        return json.loads(_DEFAULT_BIOSENSOR_FILE.read_text())
    return {
        "heart_rate": {"value": None, "ts": None, "zone": None},
        "hrv": {"value": None, "ts": None, "stress_level": None},
        "activity": {
            "move": 0,
            "exercise": 0,
            "stand": 0,
            "goal_move": 600,
            "ts": None,
        },
        "sleep": {"stage": None, "minutes": 0, "ts": None},
        "workout": {"active": False, "activity": None, "started": None},
        "last_update": None,
    }


def _save_biosensor_state(state: dict):
    _DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["last_update"] = time.time()
    _DEFAULT_BIOSENSOR_FILE.write_text(json.dumps(state, indent=2))


def _hr_zone(bpm: float) -> str:
    """Map heart rate to zone for ENDOCRINE routing."""
    if bpm < 60:
        return "resting"
    elif bpm < 80:
        return "relaxed"
    elif bpm < 100:
        return "moderate"
    elif bpm < 130:
        return "elevated"
    elif bpm < 160:
        return "high"
    else:
        return "max"


def _hrv_stress(ms: float) -> str:
    """Classify HRV into stress level (higher HRV = lower stress)."""
    if ms > 60:
        return "low"
    elif ms > 40:
        return "moderate"
    elif ms > 25:
        return "elevated"
    else:
        return "high"


RUNTIME_URL = "http://127.0.0.1:9723"


def _post_runtime(path: str, body: dict) -> bool:
    """POST to HypostasRuntime. Returns True on success."""
    import urllib.request
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{RUNTIME_URL}{path}", data=data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception as e:
        log.warning(f"Runtime POST {path} failed: {e}")
        return False


def update_endocrine_from_biometrics(state: dict):
    """
    Fire emotion events into HypostasRuntime EmotionEngine based on biometrics.
    Single source of truth — no file-direct writes.
    """
    try:
        # Heart rate → emotional state
        hr = state.get("heart_rate", {})
        zone = hr.get("zone")
        if zone == "high":
            _post_runtime("/runtime/emotion/event", {
                "event": "HR_SPIKE",
                "note": f"Heart rate elevated (zone={zone})"
            })
            log.info("HR zone=high → runtime emotion event HR_SPIKE")
        elif zone == "resting":
            _post_runtime("/runtime/emotion/event", {
                "event": "HR_RESTING",
                "note": "Heart rate resting"
            })

        # HRV → stress/calm
        hrv = state.get("hrv", {})
        stress = hrv.get("stress_level")
        if stress == "high":
            _post_runtime("/runtime/emotion/event", {
                "event": "HRV_STRESS",
                "note": f"HRV low — stress signal"
            })
            log.info("HRV stress=high → runtime emotion event HRV_STRESS")
        elif stress == "low":
            _post_runtime("/runtime/emotion/event", {
                "event": "HRV_CALM",
                "note": "HRV high — calm/recovery signal"
            })

        # Activity ring → dopamine
        activity = state.get("activity", {})
        move = activity.get("move", 0)
        goal = activity.get("goal_move", 600)
        if goal > 0 and move >= goal:
            _post_runtime("/runtime/emotion/event", {
                "event": "MOVE_RING_CLOSED",
                "note": f"Move ring closed ({move}/{goal} cal)"
            })
            log.info(f"Move ring closed → runtime emotion MOVE_RING_CLOSED")

        # Also ingest as a message for relationship/hot-tier context
        _post_runtime("/runtime/ingest", {
            "message": f"[BIOSENSOR] HR zone={zone}, HRV stress={stress}, move={move}/{goal}",
            "person": "josh",
            "channel": "biosensor",
            "direction": "received"
        })

    except Exception as e:
        log.error(f"Runtime emotion update failed: {e}")


def update_soma_from_biometrics(state: dict):
    """Legacy file-based SOMA update — kept for fallback only."""
    pass  # Now handled by update_soma_runtime() which routes to HypostasRuntime


def update_soma_runtime(state: dict):
    """Update SOMA in HypostasRuntime via HTTP — single source of truth."""
    try:
        # Build SOMA payload from biosensor state
        hr = state.get("heart_rate", {})
        hrv = state.get("hrv", {})
        activity = state.get("activity", {})
        sleep = state.get("sleep", {})
        workout = state.get("workout", {})

        # Determine posture from workout/activity
        posture = "active" if workout.get("active") else "neutral"

        # Determine energy delta from sleep/activity
        energy_delta = 0.0
        if sleep.get("stage") == "deep":
            energy_delta = +0.05  # deep sleep = recovery
            _post_runtime("/runtime/emotion/event", {"event": "DEEP_SLEEP", "note": "Deep sleep detected"})
            log.info("Deep sleep → SOMA energy +0.05, DEEP_SLEEP emotion event")
        elif workout.get("active"):
            energy_delta = -0.02  # workout = energy spend

        # POST to /runtime/soma/update (or use ingest as fallback)
        soma_payload = {
            "posture": posture,
            "energy_delta": energy_delta,
            "heart_rate": hr.get("value"),
            "hr_zone": hr.get("zone"),
            "hrv_ms": hrv.get("value"),
            "stress_level": hrv.get("stress_level"),
            "workout_active": workout.get("active", False),
            "sleep_stage": sleep.get("stage"),
            "move_calories": activity.get("move"),
            "move_goal": activity.get("goal_move"),
            "ts": time.time(),
        }

        _post_runtime("/runtime/soma/update", soma_payload)
        log.info("SOMA runtime updated from biometrics")

    except Exception as e:
        log.error(f"SOMA runtime update failed: {e}")


# ─── HTTP Handler ──────────────────────────────────────────────────────────────


class BiosensorHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.info(f"{self.address_string()} - {format % args}")

    def _respond(self, status: int, body: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _read_body(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def do_GET(self):
        if self.path == "/biosensor/status":
            state = _load_biosensor_state()
            self._respond(200, state)
        elif self.path == "/health":
            self._respond(
                200, {"status": "ok", "bridge": "biosensor_bridge", "ts": time.time()}
            )
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        body = self._read_body()
        if body is None:
            self._respond(400, {"error": "invalid JSON"})
            return

        state = _load_biosensor_state()
        path = self.path.rstrip("/")

        if path == "/biosensor/heartrate":
            bpm = float(body.get("value", 0))
            zone = _hr_zone(bpm)
            state["heart_rate"] = {"value": bpm, "ts": time.time(), "zone": zone}
            log.info(f"Heart rate: {bpm} bpm → zone={zone}")

        elif path == "/biosensor/hrv":
            ms = float(body.get("value", 0))
            stress = _hrv_stress(ms)
            state["hrv"] = {"value": ms, "ts": time.time(), "stress_level": stress}
            log.info(f"HRV: {ms} ms → stress={stress}")

        elif path == "/biosensor/activity":
            state["activity"] = {
                "move": float(body.get("move", 0)),
                "exercise": float(body.get("exercise", 0)),
                "stand": float(body.get("stand", 0)),
                "goal_move": float(body.get("goal_move", 600)),
                "ts": time.time(),
            }
            log.info(
                f"Activity: move={body.get('move')}, exercise={body.get('exercise')}, stand={body.get('stand')}"
            )

        elif path == "/biosensor/sleep":
            state["sleep"] = {
                "stage": body.get("stage", "unknown"),
                "minutes": float(body.get("minutes", 0)),
                "ts": time.time(),
            }
            log.info(f"Sleep: stage={body.get('stage')}, minutes={body.get('minutes')}")

        elif path == "/biosensor/workout":
            wtype = body.get("type", "").lower()
            if wtype == "start":
                state["workout"] = {
                    "active": True,
                    "activity": body.get("activity"),
                    "started": time.time(),
                }
                log.info(f"Workout started: {body.get('activity')}")
            elif wtype == "end":
                state["workout"] = {"active": False, "activity": None, "started": None}
                log.info("Workout ended")

        else:
            self._respond(404, {"error": f"unknown endpoint: {path}"})
            return

        _save_biosensor_state(state)
        update_endocrine_from_biometrics(state)
        update_soma_from_biometrics(state)
        update_soma_runtime(state)

        self._respond(200, {"status": "ok", "path": path, "ts": time.time()})


# ─── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Biosensor Bridge — Phase E1")
    parser.add_argument("--port", type=int, default=9721)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    _DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Biosensor bridge starting on {args.host}:{args.port}")
    log.info(f"State dir: {_DEFAULT_STATE_DIR}")
    log.info(
        "Endpoints: /biosensor/{heartrate,hrv,activity,sleep,workout} | /biosensor/status | /health"
    )

    server = HTTPServer((args.host, args.port), BiosensorHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Biosensor bridge stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
