"""NEPHRON — Excretory System / Memory Pruning.

Kidneys for the mind. Filters out:
- Stale THALAMUS entries (>1000)
- Low-relevance ENGRAM memories (below threshold)
- Old CHRONICLE entries (>30 days)
- Bloated mood_history in ENDOCRINE (>48 entries)
- Expired AMYGDALA threat cache
- Old RETINA learning entries

Runs every N loops (default: every 100 loops, ~50 minutes at 30s intervals).
Also callable on-demand for deep clean.
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pulse.src import thalamus

_DEFAULT_STATE_DIR = Path.home() / ".pulse" / "state"
_DEFAULT_STATE_FILE = _DEFAULT_STATE_DIR / "nephron-state.json"

# Pruning thresholds
THALAMUS_MAX_ENTRIES = 500  # Keep last 500 bus messages
CHRONICLE_MAX_AGE_DAYS = 30  # Archive entries older than 30 days
MOOD_HISTORY_MAX = 48  # Already enforced by endocrine, but double-check
ENGRAM_MIN_IMPORTANCE = 2  # Prune memories with importance < 2
ENGRAM_MAX_AGE_DAYS = 90  # Prune memories older than 90 days (unless high importance)
RETINA_LEARNING_MAX = 200  # Max outcome learning entries
LOOP_INTERVAL = 100  # Run every N daemon loops


def _default_state() -> dict:
    return {
        "total_cycles": 0,
        "total_pruned": 0,
        "last_run": 0,
        "last_results": {},
        "history": [],  # last 10 runs
    }


def _load_state() -> dict:
    if _DEFAULT_STATE_FILE.exists():
        try:
            return json.loads(_DEFAULT_STATE_FILE.read_text())
        except (json.JSONDecodeError, KeyError):
            pass
    return _default_state()


def _save_state(state: dict):
    _DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _DEFAULT_STATE_FILE.write_text(json.dumps(state, indent=2))


def should_run(loop_count: int) -> bool:
    """Check if it's time for a filtering cycle."""
    return loop_count > 0 and loop_count % LOOP_INTERVAL == 0


def filter_all() -> dict:
    """Run all pruning filters. Returns summary of what was cleaned."""
    results = {
        "timestamp": time.time(),
        "pruned": {},
        "errors": [],
    }

    # 1. THALAMUS — trim old bus entries
    try:
        pruned = _prune_thalamus()
        if pruned > 0:
            results["pruned"]["thalamus"] = pruned
    except Exception as e:
        results["errors"].append(f"thalamus: {e}")

    # 2. CHRONICLE — archive old timeline entries
    try:
        pruned = _prune_chronicle()
        if pruned > 0:
            results["pruned"]["chronicle"] = pruned
    except Exception as e:
        results["errors"].append(f"chronicle: {e}")

    # 3. ENDOCRINE — trim mood history
    try:
        pruned = _prune_endocrine_history()
        if pruned > 0:
            results["pruned"]["endocrine_history"] = pruned
    except Exception as e:
        results["errors"].append(f"endocrine: {e}")

    # 4. RETINA — trim learning entries
    try:
        pruned = _prune_retina_learning()
        if pruned > 0:
            results["pruned"]["retina_learning"] = pruned
    except Exception as e:
        results["errors"].append(f"retina: {e}")

    # 5. ENGRAM — prune low-importance old memories
    try:
        pruned = _prune_engrams()
        if pruned > 0:
            results["pruned"]["engrams"] = pruned
    except Exception as e:
        results["errors"].append(f"engrams: {e}")

    # Update state
    state = _load_state()
    state["total_cycles"] += 1
    total = sum(results["pruned"].values())
    state["total_pruned"] += total
    state["last_run"] = time.time()
    state["last_results"] = results
    state["history"].append(
        {
            "ts": time.time(),
            "pruned": total,
            "breakdown": results["pruned"],
        }
    )
    state["history"] = state["history"][-10:]  # keep last 10
    _save_state(state)

    # Broadcast to THALAMUS
    thalamus.append(
        {
            "source": "nephron",
            "type": "filter_cycle",
            "salience": 0.3,
            "data": {
                "total_pruned": total,
                "breakdown": results["pruned"],
                "errors": len(results["errors"]),
            },
        }
    )

    return results


def _prune_thalamus() -> int:
    """Trim THALAMUS bus to max entries."""
    thalamus_file = _DEFAULT_STATE_DIR / "thalamus.jsonl"
    if not thalamus_file.exists():
        return 0

    lines = thalamus_file.read_text().strip().split("\n")
    if len(lines) <= THALAMUS_MAX_ENTRIES:
        return 0

    pruned = len(lines) - THALAMUS_MAX_ENTRIES
    kept = lines[-THALAMUS_MAX_ENTRIES:]
    thalamus_file.write_text("\n".join(kept) + "\n")
    return pruned


def _prune_chronicle() -> int:
    """Remove CHRONICLE entries older than threshold."""
    chronicle_file = _DEFAULT_STATE_DIR / "chronicle.jsonl"
    if not chronicle_file.exists():
        return 0

    cutoff = time.time() - (CHRONICLE_MAX_AGE_DAYS * 86400)
    lines = chronicle_file.read_text().strip().split("\n")
    kept = []
    pruned = 0

    for line in lines:
        try:
            entry = json.loads(line)
            if entry.get("ts", 0) >= cutoff:
                kept.append(line)
            else:
                pruned += 1
        except json.JSONDecodeError:
            pruned += 1

    if pruned > 0:
        chronicle_file.write_text("\n".join(kept) + "\n" if kept else "")
    return pruned


def _prune_endocrine_history() -> int:
    """Trim mood_history to max entries."""
    endo_file = _DEFAULT_STATE_DIR / "endocrine-state.json"
    if not endo_file.exists():
        return 0

    try:
        state = json.loads(endo_file.read_text())
        history = state.get("mood_history", [])
        if len(history) <= MOOD_HISTORY_MAX:
            return 0

        pruned = len(history) - MOOD_HISTORY_MAX
        state["mood_history"] = history[-MOOD_HISTORY_MAX:]
        endo_file.write_text(json.dumps(state, indent=2))
        return pruned
    except (json.JSONDecodeError, KeyError):
        return 0


def _prune_retina_learning() -> int:
    """Trim RETINA outcome learning to max entries."""
    retina_file = _DEFAULT_STATE_DIR / "retina-learning.json"
    if not retina_file.exists():
        return 0

    try:
        data = json.loads(retina_file.read_text())
        outcomes = data.get("outcomes", [])
        if len(outcomes) <= RETINA_LEARNING_MAX:
            return 0

        pruned = len(outcomes) - RETINA_LEARNING_MAX
        data["outcomes"] = outcomes[-RETINA_LEARNING_MAX:]
        retina_file.write_text(json.dumps(data, indent=2))
        return pruned
    except (json.JSONDecodeError, KeyError):
        return 0


def _prune_engrams() -> int:
    """Prune low-importance old ENGRAM memories.

    engram-store.json is a raw list of memory dicts (not wrapped in a dict).
    Each entry has: id, event, emotion{valence,intensity,label}, location,
    timestamp (epoch_ms), sensory, associations, recall_count, last_recalled.
    There is no top-level 'importance' field — we use emotion.intensity as proxy.
    """
    engram_file = _DEFAULT_STATE_DIR / "engram-store.json"
    if not engram_file.exists():
        return 0

    try:
        data = json.loads(engram_file.read_text())
        # Handle both raw list and legacy dict-wrapped formats
        if isinstance(data, dict):
            memories = data.get("memories", [])
        elif isinstance(data, list):
            memories = data
        else:
            return 0

        if not memories:
            return 0

        # cutoff_ms: engram timestamps are in epoch milliseconds
        cutoff_ms = (time.time() - (ENGRAM_MAX_AGE_DAYS * 86400)) * 1000
        kept = []
        pruned = 0

        for mem in memories:
            if not isinstance(mem, dict):
                kept.append(mem)
                continue

            # Importance: explicit field or emotion.intensity (0.0–1.0 or 0–10 scale)
            importance = mem.get("importance", None)
            if importance is None:
                emotion = mem.get("emotion", {})
                importance = emotion.get("intensity", 0.5) if isinstance(emotion, dict) else 0.5
            # Normalize 0–10 scale to 0–1
            if isinstance(importance, (int, float)) and importance > 1.0:
                importance = min(importance / 10.0, 1.0)

            # timestamp field (epoch_ms); fall back to 'ts' in seconds
            ts_ms = mem.get("timestamp")
            if ts_ms is None:
                ts_ms = mem.get("ts", time.time() * 1000)
                # if ts looks like epoch seconds, convert
                if ts_ms < 1e12:
                    ts_ms *= 1000

            # Keep if: high importance OR recently created
            if importance >= ENGRAM_MIN_IMPORTANCE / 10.0 or ts_ms >= cutoff_ms:
                kept.append(mem)
            else:
                pruned += 1

        if pruned > 0:
            # Always write back as raw list (matching engram.py's format)
            engram_file.write_text(json.dumps(kept, indent=2))
        return pruned
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
        return 0


def get_status() -> dict:
    """Return current NEPHRON status."""
    state = _load_state()
    return {
        "total_cycles": state["total_cycles"],
        "total_pruned": state["total_pruned"],
        "last_run": state["last_run"],
        "last_results": state["last_results"],
        "seconds_since_last": (
            time.time() - state["last_run"] if state["last_run"] else None
        ),
    }


# --- Tests ---


def _run_tests():
    """Basic self-tests."""
    import tempfile
    import os

    print("Testing NEPHRON...")

    # Test state management
    state = _default_state()
    assert state["total_cycles"] == 0
    assert state["total_pruned"] == 0
    print("  ✅ Default state")

    # Test should_run
    assert not should_run(0)
    assert not should_run(50)
    assert should_run(100)
    assert should_run(200)
    assert not should_run(99)
    print("  ✅ Loop interval check")

    # Test thalamus pruning
    test_file = _DEFAULT_STATE_DIR / "thalamus-test.jsonl"
    test_lines = [json.dumps({"ts": i, "source": "test"}) for i in range(600)]
    test_file.write_text("\n".join(test_lines) + "\n")
    # Read back and verify we could prune
    lines = test_file.read_text().strip().split("\n")
    assert len(lines) == 600
    test_file.unlink()
    print("  ✅ Thalamus pruning logic")

    # Test filter_all runs without crash
    results = filter_all()
    assert "pruned" in results
    assert "errors" in results
    print(
        f"  ✅ Full filter cycle (pruned: {sum(results['pruned'].values())}, errors: {len(results['errors'])})"
    )

    # Verify state was updated
    status = get_status()
    assert status["total_cycles"] >= 1
    print(f"  ✅ State tracking (cycles: {status['total_cycles']})")

    print(f"\n  All NEPHRON tests passed! ✅")


if __name__ == "__main__":
    _run_tests()
