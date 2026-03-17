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


def _qty(val) -> Optional[float]:
    """Extract numeric qty from Health Auto Export value (dict or number)."""
    if isinstance(val, dict):
        return val.get("qty")
    return val


def _parse_hae_workout(workout_dict: dict) -> dict:
    """Parse a single Health Auto Export v2 workout object into a Pulse workout state dict.

    Handles both v1 (plain numeric fields) and v2 ({"qty": X, "units": "..."} wrapper)
    field formats, plus multiple field-name aliases used across HAE versions.

    Returns a workout state dict with keys: active, activity, duration_min, calories,
    avg_hr, distance, started.
    """
    activity = (
        workout_dict.get("name")
        or workout_dict.get("workoutActivityType")
        or workout_dict.get("activityType")
        or workout_dict.get("type")
        or "unknown"
    )

    # duration: prefer durationMin (minutes), then duration (may be seconds if > 1440)
    dur = workout_dict.get("durationMin") or workout_dict.get("duration")
    if dur is not None and dur > 1440:  # sanity: > 1440 min is almost certainly seconds
        dur = round(dur / 60, 1)

    cal_raw = (
        _qty(workout_dict.get("activeEnergyBurned"))
        or _qty(workout_dict.get("totalEnergyBurned"))
        or _qty(workout_dict.get("calories"))
    )
    cal = round(float(cal_raw), 1) if cal_raw is not None else None

    avg_hr = _qty(workout_dict.get("averageHeartRate")) or _qty(workout_dict.get("avgHeartRate"))

    return {
        "active": False,
        "activity": activity,
        "duration_min": dur,
        "calories": cal,
        "avg_hr": avg_hr,
        "distance": workout_dict.get("totalDistance") or workout_dict.get("distance"),
        "started": workout_dict.get("date") or workout_dict.get("startDate"),
    }


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
        body_data = state.get("body", {})
        nutrition_data = state.get("nutrition", {})
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
            "steps": activity.get("steps"),
            "weight_lbs": body_data.get("weight_lbs"),
            "body_fat_pct": body_data.get("body_fat_pct"),
            "bmi": body_data.get("bmi"),
            "vo2_max": body_data.get("vo2_max"),
            "water_ml": nutrition_data.get("water_ml"),
            "calories": nutrition_data.get("calories"),
            "blood_oxygen": state.get("blood_oxygen", {}).get("value"),
            "respiratory_rate": state.get("respiratory_rate", {}).get("value"),
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

        elif path == "/biosensor/batch":
            # Health Auto Export format: {"data": {"metrics": [...], "workouts": [...]}}
            data_obj = body.get("data", {})
            metrics_list = data_obj.get("metrics", [])

            # Handle top-level workouts array (separate Health Auto Export automation)
            top_level_workouts = data_obj.get("workouts", [])
            if top_level_workouts:
                try:
                    latest_w = sorted(top_level_workouts, key=lambda x: x.get("date", ""), reverse=True)[0]
                    log.info(f"[batch] Raw workout keys: {list(latest_w.keys())}")
                    state["workout"] = _parse_hae_workout(latest_w)
                    log.info(f"[batch] Workout (top-level): {state['workout']['activity']}, {state['workout']['duration_min']} min, {state['workout']['calories']} cal")
                except Exception as e:
                    log.warning(f"[batch] Failed to parse top-level workouts: {e}")

            # Build a flat lookup: metric_name → latest qty value
            def latest_qty(entries):
                """Get the most recent numeric qty from a list of {date, qty} entries.
                Returns None for non-numeric qty (e.g. sleep stage strings handled separately)."""
                if not entries:
                    return None
                try:
                    sorted_entries = sorted(entries, key=lambda x: x.get("date", ""), reverse=True)
                    val = sorted_entries[0].get("qty")
                    # Only return numeric values — string stage names belong to sleep handling
                    if isinstance(val, (int, float)):
                        return val
                    return None
                except Exception:
                    return None

            # ── Sleep parsing (HAE-specific: qty is a string stage name, not a duration) ──
            def _parse_hae_sleep(metrics_list):
                """
                HAE sends sleep_analysis entries where each data point is:
                  {"date": "2026-03-17 ...", "qty": "asleep" | "inBed" | "awake" | "core" | "deep" | "rem"}
                  with an optional "duration" field (seconds) or a paired start/end.

                We aggregate total asleep minutes and pick the dominant stage.
                """
                STAGE_MAP = {
                    "asleep":      "unknown",
                    "inbed":       "inBed",
                    "awake":       "awake",
                    "core":        "core",
                    "deep":        "deep",
                    "rem":         "rem",
                    "restless":    "core",  # older Watch firmware
                    "unspecified": "unknown",
                }
                ASLEEP_STAGES = {"asleep", "core", "deep", "rem", "restless", "unspecified"}

                for m in metrics_list:
                    if m.get("name") != "sleep_analysis":
                        continue
                    entries = m.get("data", [])
                    if not entries:
                        return None

                    total_asleep_min = 0.0
                    stage_minutes = {}

                    for e in entries:
                        raw_stage = str(e.get("qty", "")).lower()
                        stage = STAGE_MAP.get(raw_stage, "unknown")

                        # Duration: try explicit field first, then infer from startDate/endDate
                        dur_sec = e.get("duration")  # seconds (some HAE versions)
                        if dur_sec is None:
                            val = e.get("value")
                            if val is not None:
                                dur_sec = float(val) * 60  # assume minutes → seconds
                        if dur_sec is None:
                            # HAE most commonly uses startDate/endDate strings
                            start = e.get("startDate") or e.get("start_date") or e.get("startTime")
                            end   = e.get("endDate")   or e.get("end_date")   or e.get("endTime")
                            if start and end:
                                try:
                                    from datetime import datetime
                                    FMT = "%Y-%m-%d %H:%M:%S %z"
                                    t0 = datetime.strptime(start, FMT)
                                    t1 = datetime.strptime(end,   FMT)
                                    dur_sec = (t1 - t0).total_seconds()
                                except Exception:
                                    pass
                        if dur_sec is None:
                            continue  # can't compute duration for this entry

                        dur_min = float(dur_sec) / 60.0

                        if raw_stage in ASLEEP_STAGES:
                            total_asleep_min += dur_min
                        stage_minutes[stage] = stage_minutes.get(stage, 0) + dur_min

                    if total_asleep_min == 0 and stage_minutes:
                        # All entries lack duration — just count entries as proxy (1 entry ≈ 1 min)
                        # and note it as inaccurate
                        total_asleep_min = sum(stage_minutes.values())
                        log.warning("[batch] Sleep: no duration field found — using entry count as minute estimate")

                    dominant_stage = max(stage_minutes, key=stage_minutes.get) if stage_minutes else "unknown"
                    log.info(f"[batch] Sleep: {total_asleep_min:.1f} min asleep, dominant={dominant_stage}, breakdown={stage_minutes}")
                    return {"stage": dominant_stage, "minutes": round(total_asleep_min, 1)}

                return None  # sleep_analysis metric not present in payload

            metrics = {}
            for m in metrics_list:
                name = m.get("name", "")
                qty = latest_qty(m.get("data", []))
                if qty is not None:
                    metrics[name] = qty

            log.info(f"[batch] parsed metrics: {list(metrics.keys())}")

            # Heart rate
            hr = metrics.get("heart_rate")
            if hr:
                zone = _hr_zone(float(hr))
                state["heart_rate"] = {"value": float(hr), "ts": time.time(), "zone": zone}
                log.info(f"[batch] Heart rate: {hr} bpm → zone={zone}")

            # HRV
            hrv = metrics.get("heart_rate_variability")
            if hrv:
                stress = _hrv_stress(float(hrv))
                state["hrv"] = {"value": float(hrv), "ts": time.time(), "stress_level": stress}
                log.info(f"[batch] HRV: {hrv} ms → stress={stress}")

            # Resting heart rate
            rhr = metrics.get("resting_heart_rate")
            if rhr:
                state["resting_heart_rate"] = {"value": float(rhr), "ts": time.time()}
                log.info(f"[batch] Resting HR: {rhr} bpm")

            # Active energy + steps
            energy = metrics.get("active_energy")
            steps = metrics.get("step_count") or metrics.get("steps")
            stand = metrics.get("apple_stand_hour") or metrics.get("apple_stand_time")
            if energy or steps:
                state["activity"] = {
                    "move": float(energy or 0),
                    "steps": float(steps or 0),
                    "stand": float(stand or 0),
                    "exercise": float(metrics.get("apple_exercise_time", 0) or 0),
                    "goal_move": 600,
                    "ts": time.time(),
                }
                log.info(f"[batch] Activity: energy={energy}, steps={steps}, stand={stand}")

            # Blood oxygen
            spo2 = metrics.get("blood_oxygen_saturation")
            if spo2:
                state["blood_oxygen"] = {"value": float(spo2), "ts": time.time()}
                log.info(f"[batch] SpO2: {spo2}%")

            # Sleep — HAE sends stage-string entries, not scalar qty; use dedicated parser
            sleep_parsed = _parse_hae_sleep(metrics_list)
            if sleep_parsed:
                state["sleep"] = {
                    "stage": sleep_parsed["stage"],
                    "minutes": sleep_parsed["minutes"],
                    "ts": time.time(),
                }
            else:
                # Fallback: scalar fields from other sources
                sleep_raw = (
                    metrics.get("sleep_analysis")
                    or metrics.get("time_in_bed")
                    or metrics.get("sleep_duration")
                    or metrics.get("sleep")
                )
                if sleep_raw is not None:
                    try:
                        sleep_mins = float(sleep_raw)
                        if sleep_mins > 60 * 48:
                            sleep_mins = round(sleep_mins / 60.0, 1)
                        state["sleep"] = {"stage": "unknown", "minutes": sleep_mins, "ts": time.time()}
                        log.info(f"[batch] Sleep (scalar fallback): {sleep_mins} min")
                    except Exception as e:
                        log.warning(f"[batch] Sleep scalar parse failed: {e}")

            # Weight / body mass
            weight_kg = (
                metrics.get("body_mass")
                or metrics.get("weight_body_mass")
                or metrics.get("weight")
                or metrics.get("weight_kg")
            )
            if weight_kg:
                state["body"] = state.get("body", {})
                state["body"]["weight_kg"] = float(weight_kg)
                state["body"]["weight_lbs"] = round(float(weight_kg) * 2.20462, 1)
                state["body"]["weight_ts"] = time.time()
                log.info(f"[batch] Weight: {weight_kg} kg ({state['body']['weight_lbs']} lbs)")

            # Body fat percentage
            body_fat = metrics.get("body_fat_percentage")
            if body_fat:
                state["body"] = state.get("body", {})
                state["body"]["body_fat_pct"] = float(body_fat)
                state["body"]["body_fat_ts"] = time.time()
                log.info(f"[batch] Body fat: {body_fat}%")

            # BMI
            bmi = metrics.get("body_mass_index")
            if bmi:
                state["body"] = state.get("body", {})
                state["body"]["bmi"] = float(bmi)
                log.info(f"[batch] BMI: {bmi}")

            # Water intake
            water = metrics.get("dietary_water")
            if water:
                state["nutrition"] = state.get("nutrition", {})
                state["nutrition"]["water_ml"] = float(water)
                state["nutrition"]["water_ts"] = time.time()
                log.info(f"[batch] Water: {water} ml")

            # Nutrition
            calories = metrics.get("dietary_energy")
            protein = metrics.get("dietary_protein")
            carbs = metrics.get("dietary_carbohydrates")
            fat = metrics.get("dietary_fat_total")
            if any([calories, protein, carbs, fat]):
                state["nutrition"] = state.get("nutrition", {})
                if calories: state["nutrition"]["calories"] = float(calories)
                if protein: state["nutrition"]["protein_g"] = float(protein)
                if carbs: state["nutrition"]["carbs_g"] = float(carbs)
                if fat: state["nutrition"]["fat_g"] = float(fat)
                state["nutrition"]["nutrition_ts"] = time.time()
                log.info(f"[batch] Nutrition: cal={calories}, protein={protein}g, carbs={carbs}g, fat={fat}g")

            # Workouts
            workout_data = None
            for m in metrics_list:
                if m.get("name") == "workouts":
                    entries = m.get("data", [])
                    if entries:
                        latest = sorted(entries, key=lambda x: x.get("date",""), reverse=True)[0]
                        workout_data = latest
                    break
            if workout_data:
                state["workout"] = {
                    "active": False,
                    "activity": workout_data.get("workoutActivityType", "unknown"),
                    "duration_min": workout_data.get("duration"),
                    "calories": workout_data.get("totalEnergyBurned"),
                    "avg_hr": workout_data.get("averageHeartRate"),
                    "distance": workout_data.get("totalDistance"),
                    "started": workout_data.get("date"),
                }
                log.info(f"[batch] Workout: {state['workout']['activity']}, {state['workout']['duration_min']} min, {state['workout']['calories']} cal")

            # Mindful minutes
            mindful = metrics.get("mindful_minutes") or metrics.get("mindfulness")
            if mindful:
                state["mindfulness"] = {"minutes": float(mindful), "ts": time.time()}
                log.info(f"[batch] Mindful: {mindful} min")

            # Respiratory rate
            resp_rate = metrics.get("respiratory_rate")
            if resp_rate:
                state["respiratory_rate"] = {"value": float(resp_rate), "ts": time.time()}
                log.info(f"[batch] Respiratory rate: {resp_rate} breaths/min")

            # Vo2 max
            vo2 = metrics.get("vo2_max") or metrics.get("cardio_fitness")
            if vo2:
                state["body"] = state.get("body", {})
                state["body"]["vo2_max"] = float(vo2)
                log.info(f"[batch] VO2 max: {vo2}")

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
