# Pulse Instincts System — v0.5.0 Build Spec

## What Is This

Instincts are Pulse's autonomous skill system. Unlike OpenClaw/Claude skills (which are triggered by
human conversation), Instincts are triggered by **drive state + context**. They fire autonomously
when the right combination of drives spike and environmental conditions are met.

Think of it as: drives are the urge, instincts are the pre-programmed expert behavior that satisfies it.

## Design Principles

- **Deterministic over stochastic**: Instincts run scripts with embedded rules. GENERATE synthesizes
  LLM tasks. Instincts fire first (reliable), GENERATE is fallback (creative).
- **Drive-gated**: No instinct fires unless matching drives are above threshold.
- **Context-aware**: Time of day, system state, external conditions can gate an instinct.
- **Cooldown-protected**: Each instinct has a minimum interval between firings.
- **Self-contained**: Each instinct is a folder with INSTINCT.md + scripts + references.

## Architecture Overview

```
pulse/
  instincts/                         ← New: instinct definitions live here
    weather-market-scan/
      INSTINCT.md
      scan.py                        ← lightweight wrapper script
    memory-maintenance/
      INSTINCT.md
      consolidate.py
    x-engagement/
      INSTINCT.md
      engage.py
  src/
    instincts/                       ← New: Python package
      __init__.py
      loader.py                      ← Parse INSTINCT.md frontmatter + body
      registry.py                    ← InstinctRegistry: load + match instincts
      executor.py                    ← InstinctExecutor: run, cooldown, route output
      models.py                      ← Instinct dataclass + InstinctResult
    core/
      daemon.py                      ← MODIFY: wire registry + executor, check before GENERATE
```

## INSTINCT.md Format

Each instinct folder must contain an INSTINCT.md with YAML frontmatter:

```yaml
---
name: weather-market-scan
description: Scan Polymarket weather markets during GFS data windows for FLOOR/CEILING opportunities
version: "1.0"
enabled: true

triggers:
  drives:
    curiosity: ">= 3.0"
    financial_urgency: ">= 2.0"
  context:
    gfs_window: true          # Only fire near GFS availability windows (UTC 02,08,14,20)

cooldown_minutes: 90
timeout_seconds: 120

output:
  log: true                   # Always write to daily notes
  discord: "edge-alerts"      # Optional: post results to this Discord channel key
  signal: false               # Optional: send to Signal

script: scan.py               # Relative to instinct folder
---

# Weather Market Scan

Full instructions/context here in markdown. Loaded into script environment as INSTINCT_BODY env var.

When drives fire near a GFS data window, scan Polymarket weather temperature markets.
Focus on FLOOR ("X or below") and CEILING ("X or higher") directional markets only.
Skip exact 1-degree brackets.

Priority cities: NYC, London, Paris, Seattle, Chicago, Dallas, Atlanta, Miami.
Entry rules: YES if price < 0.15, NO if price > 0.45. Max $2 per bet. Max 2/city.
```

## Task 1: Create `pulse/src/instincts/models.py`

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any


@dataclass
class InstinctTrigger:
    drives: dict[str, str]   # {"curiosity": ">= 3.0", "financial_urgency": ">= 2.0"}
    context: dict[str, Any]  # {"gfs_window": True}


@dataclass
class InstinctOutput:
    log: bool = True
    discord: Optional[str] = None   # channel key e.g. "edge-alerts"
    signal: bool = False


@dataclass
class Instinct:
    name: str
    description: str
    version: str
    enabled: bool
    triggers: InstinctTrigger
    cooldown_minutes: int
    timeout_seconds: int
    output: InstinctOutput
    script: str             # filename relative to instinct folder
    body: str               # markdown body of INSTINCT.md
    path: Path              # absolute path to instinct folder


@dataclass
class InstinctResult:
    instinct_name: str
    success: bool
    output: str
    error: Optional[str]
    duration_seconds: float
    fired_at: float         # unix timestamp
```

## Task 2: Create `pulse/src/instincts/loader.py`

Parse INSTINCT.md files. Use the `python-frontmatter` or `pyyaml` library (already in Pulse deps).
If neither is available, use a simple regex approach to split `---` frontmatter from body.

```python
from pathlib import Path
from pulse.src.instincts.models import Instinct, InstinctTrigger, InstinctOutput


def load_instinct(folder: Path) -> Optional[Instinct]:
    """
    Load an instinct from a folder containing INSTINCT.md.
    Returns None if INSTINCT.md is missing, malformed, or enabled=false.
    """
    ...


def load_all_instincts(instincts_dir: Path) -> list[Instinct]:
    """
    Load all instincts from subdirectories of instincts_dir.
    Skips folders without INSTINCT.md. Logs warnings for malformed instincts.
    """
    ...
```

## Task 3: Create `pulse/src/instincts/registry.py`

```python
from pathlib import Path
from pulse.src.instincts.models import Instinct
from pulse.src.instincts.loader import load_all_instincts


class InstinctRegistry:
    def __init__(self, instincts_dir: Path):
        self.instincts_dir = instincts_dir
        self._instincts: list[Instinct] = load_all_instincts(instincts_dir)

    def match(self, drive_state: dict[str, float], context: dict) -> list[Instinct]:
        """
        Return instincts whose trigger conditions are satisfied.

        drive_state: {"curiosity": 3.5, "financial_urgency": 2.1, ...}
        context: {"gfs_window": True, "hour_utc": 8, ...}

        For drive conditions, parse strings like ">= 3.0", "> 2", "== 5":
          - Split operator and value
          - Compare drive_state.get(drive_name, 0) against value using operator

        For context conditions:
          - Simple equality: context.get(key) == value

        Return only instincts where ALL drive AND context conditions match.
        Sort by sum of drive pressures (highest urgency first).
        """
        ...

    def _evaluate_condition(self, condition: str, actual: float) -> bool:
        """Parse and evaluate '>= 3.0' style condition strings."""
        condition = condition.strip()
        for op in (">=", "<=", "!=", ">", "<", "=="):
            if condition.startswith(op):
                threshold = float(condition[len(op):].strip())
                if op == ">=": return actual >= threshold
                if op == "<=": return actual <= threshold
                if op == ">":  return actual > threshold
                if op == "<":  return actual < threshold
                if op == "==": return actual == threshold
                if op == "!=": return actual != threshold
        return False

    def all_instincts(self) -> list[Instinct]:
        return list(self._instincts)

    def reload(self):
        """Reload all instincts from disk."""
        self._instincts = load_all_instincts(self.instincts_dir)
```

## Task 4: Create `pulse/src/instincts/executor.py`

```python
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from pulse.src.instincts.models import Instinct, InstinctResult


COOLDOWN_STATE_FILE = Path.home() / ".pulse" / "instinct_cooldowns.json"


class InstinctExecutor:
    def __init__(self, state_file: Path = COOLDOWN_STATE_FILE):
        self.state_file = state_file
        self._cooldowns: dict[str, float] = self._load()

    def is_ready(self, instinct: Instinct) -> bool:
        """Return True if the instinct's cooldown has elapsed."""
        last_fired = self._cooldowns.get(instinct.name, 0.0)
        elapsed_minutes = (time.time() - last_fired) / 60
        return elapsed_minutes >= instinct.cooldown_minutes

    def execute(self, instinct: Instinct, context: dict) -> InstinctResult:
        """
        Run the instinct script as a subprocess.

        - Sets env vars: INSTINCT_NAME, INSTINCT_BODY, PULSE_CONTEXT (json)
        - Runs script from the instinct's folder as cwd
        - Captures stdout+stderr
        - Records cooldown on completion (success or failure)
        - Returns InstinctResult
        """
        script_path = instinct.path / instinct.script
        if not script_path.exists():
            return InstinctResult(
                instinct_name=instinct.name,
                success=False,
                output="",
                error=f"Script not found: {script_path}",
                duration_seconds=0.0,
                fired_at=time.time(),
            )

        env = os.environ.copy()
        env["INSTINCT_NAME"] = instinct.name
        env["INSTINCT_BODY"] = instinct.body
        env["PULSE_CONTEXT"] = json.dumps(context)

        start = time.time()
        try:
            result = subprocess.run(
                ["python3", str(script_path)],
                capture_output=True,
                text=True,
                timeout=instinct.timeout_seconds,
                cwd=str(instinct.path),
                env=env,
            )
            duration = time.time() - start
            success = result.returncode == 0
            output = result.stdout + (result.stderr if not success else "")
        except subprocess.TimeoutExpired:
            duration = time.time() - start
            success = False
            output = f"Instinct timed out after {instinct.timeout_seconds}s"
        except Exception as e:
            duration = time.time() - start
            success = False
            output = str(e)

        # Record cooldown regardless of success
        self._cooldowns[instinct.name] = time.time()
        self._save()

        return InstinctResult(
            instinct_name=instinct.name,
            success=success,
            output=output,
            error=None if success else output,
            duration_seconds=duration,
            fired_at=self._cooldowns[instinct.name],
        )

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except Exception:
                return {}
        return {}

    def _save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self._cooldowns, indent=2))
```

## Task 5: Create `pulse/src/instincts/__init__.py`

Export main classes:
```python
from .registry import InstinctRegistry
from .executor import InstinctExecutor
from .models import Instinct, InstinctResult

__all__ = ["InstinctRegistry", "InstinctExecutor", "Instinct", "InstinctResult"]
```

## Task 6: Wire into `pulse/src/core/daemon.py`

In `__init__` or `setup()`, initialize registry and executor:
```python
from pulse.src.instincts import InstinctRegistry, InstinctExecutor

# In __init__:
instincts_dir = Path(__file__).parent.parent.parent / "instincts"
if instincts_dir.exists():
    self.instinct_registry = InstinctRegistry(instincts_dir)
    self.instinct_executor = InstinctExecutor()
else:
    self.instinct_registry = None
    self.instinct_executor = None
```

In `_maybe_generate()`, BEFORE calling `germinal_generate()`:
```python
# INSTINCT CHECK — fire matching instincts before falling back to LLM synthesis
if self.instinct_registry and self.instinct_executor:
    # Build context for matching
    try:
        from weather_edge_gfs_timer import is_near_gfs_window
        gfs_window = is_near_gfs_window()
    except ImportError:
        gfs_window = False

    context = {
        "gfs_window": gfs_window,
        "hour_utc": datetime.utcnow().hour,
    }

    matching = self.instinct_registry.match(drives_dict, context)
    fired_any = False
    for instinct in matching:
        if self.instinct_executor.is_ready(instinct):
            logger.info(f"INSTINCT: firing {instinct.name}")
            result = self.instinct_executor.execute(instinct, context)
            if result.success:
                logger.info(f"INSTINCT {instinct.name}: completed in {result.duration_seconds:.1f}s")
            else:
                logger.warning(f"INSTINCT {instinct.name}: failed — {result.error}")

            # Log to daily notes (same pattern as GENERATE)
            if self.daily_sync:
                try:
                    path = self.daily_sync._get_file()
                    now_str = datetime.now().strftime("%H:%M")
                    with open(path, "a") as f:
                        import fcntl
                        fcntl.flock(f, fcntl.LOCK_EX)
                        try:
                            icon = "✅" if result.success else "❌"
                            f.write(f"- {now_str} {icon} INSTINCT: {instinct.name} fired\n")
                            if result.output:
                                for line in result.output.strip().split("\n")[:5]:
                                    f.write(f"  {line}\n")
                        finally:
                            fcntl.flock(f, fcntl.LOCK_UN)
                except Exception as e:
                    logger.warning(f"Failed to log instinct to daily notes: {e}")
            fired_any = True

    if fired_any:
        self._last_generate_time = now  # suppress GENERATE if instincts ran
        return
```

## Task 7: Create Initial Instincts

### `pulse/instincts/weather-market-scan/INSTINCT.md`
```yaml
---
name: weather-market-scan
description: Scan Polymarket weather markets for FLOOR/CEILING directional opportunities during GFS windows
version: "1.0"
enabled: true

triggers:
  drives:
    curiosity: ">= 3.0"
    financial_urgency: ">= 2.0"
  context:
    gfs_window: true

cooldown_minutes: 90
timeout_seconds: 120

output:
  log: true
  discord: "edge-alerts"
  signal: false

script: scan.py
---

Scan Polymarket weather temperature markets. Focus on FLOOR and CEILING markets only.
Priority cities: NYC, London, Paris, Seattle, Chicago, Dallas.
YES if price < 0.15. NO if price > 0.45. Max $2/bet. Max 2 bets/city.
```

### `pulse/instincts/weather-market-scan/scan.py`
```python
#!/usr/bin/env python3
"""Weather market scan instinct — called by Pulse when drives + GFS window align."""
import sys
import os
import json
import requests

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
PRIORITY_CITIES = ["london", "new york", "nyc", "seattle", "chicago", "dallas", "paris", "miami"]

def classify_market_type(question: str) -> str:
    q = question.lower()
    if "or below" in q or "or less" in q:
        return "FLOOR"
    if "or higher" in q or "or above" in q or "or more" in q:
        return "CEILING"
    if "between" in q:
        return "RANGE"
    return "EXACT"

def main():
    print(f"🌤️  Weather Market Scan — {os.environ.get('INSTINCT_NAME', 'instinct')}")
    context = json.loads(os.environ.get("PULSE_CONTEXT", "{}"))
    print(f"Context: gfs_window={context.get('gfs_window')}, hour_utc={context.get('hour_utc')}")

    try:
        resp = requests.get(
            GAMMA_URL,
            params={"active": "true", "closed": "false", "tag_slug": "weather", "limit": 100},
            timeout=15,
        )
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:
        print(f"ERROR fetching markets: {e}", file=sys.stderr)
        return 1

    opportunities = []
    for m in markets:
        question = m.get("question", "")
        q_lower = question.lower()

        # Filter to priority cities
        if not any(city in q_lower for city in PRIORITY_CITIES):
            continue

        market_type = classify_market_type(question)
        if market_type == "EXACT":
            continue  # Skip exact brackets

        # Parse YES price
        try:
            prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
            yes_price = float(prices[0])
        except Exception:
            continue

        if yes_price < 0.15:
            opportunities.append({"type": market_type, "side": "YES", "price": yes_price, "question": question})
        elif yes_price > 0.45:
            opportunities.append({"type": market_type, "side": "NO", "price": 1 - yes_price, "question": question})

    if opportunities:
        print(f"\n🎯 Found {len(opportunities)} opportunities:")
        for opp in opportunities:
            icon = "🟢" if opp["side"] == "YES" else "🔴"
            print(f"  {icon} [{opp['type']}] {opp['side']} @ {opp['price']:.2f} — {opp['question'][:80]}")
    else:
        print("No opportunities found at current prices.")

    return 0

if __name__ == "__main__":
    sys.exit(main())
```

### `pulse/instincts/memory-maintenance/INSTINCT.md`
```yaml
---
name: memory-maintenance
description: Consolidate and review daily memory files when idle and drives are low
version: "1.0"
enabled: true

triggers:
  drives:
    curiosity: ">= 1.0"
  context:
    gfs_window: false   # Run when NOT near trading windows

cooldown_minutes: 360   # 6 hours between runs

output:
  log: true

script: consolidate.py
---

Review recent daily memory files and summarize key events.
Look for patterns, decisions, and lessons worth capturing long-term.
Output a brief summary of what was found.
```

### `pulse/instincts/memory-maintenance/consolidate.py`
```python
#!/usr/bin/env python3
"""Memory maintenance instinct — review daily notes and summarize."""
import os
import json
from pathlib import Path
from datetime import datetime, timedelta

def main():
    print("🧠 Memory Maintenance Instinct")
    workspace = Path.home() / ".openclaw" / "workspace"
    memory_dir = workspace / "memory"

    if not memory_dir.exists():
        print("No memory directory found")
        return 0

    # Find recent memory files (last 3 days)
    today = datetime.now().date()
    files_read = []
    total_lines = 0
    for i in range(3):
        date = today - timedelta(days=i)
        path = memory_dir / f"{date}.md"
        if path.exists():
            content = path.read_text()
            lines = len(content.splitlines())
            total_lines += lines
            files_read.append(f"{date}: {lines} lines")

    print(f"📚 Reviewed {len(files_read)} memory files ({total_lines} total lines):")
    for f in files_read:
        print(f"  - {f}")

    if total_lines > 100:
        print("⚠️  Large memory accumulation — consider updating MEMORY.md with key insights")
    else:
        print("✅ Memory files look healthy")

    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main())
```

## Task 8: Write Tests

Create `pulse/tests/unit/test_instincts/test_registry.py`:

Test that:
- InstinctRegistry loads instincts from a temp directory with mock INSTINCT.md files
- `match()` returns instincts when drives satisfy conditions
- `match()` returns empty list when drives below threshold
- `_evaluate_condition()` correctly handles >=, <=, >, <, ==, !=
- Disabled instincts (enabled: false) are not returned by match()
- Context conditions (gfs_window: true) are correctly checked

Create `pulse/tests/unit/test_instincts/test_executor.py`:

Test that:
- `is_ready()` returns True when cooldown not set
- `is_ready()` returns False when within cooldown window
- `is_ready()` returns True when cooldown elapsed
- `execute()` runs a simple script and returns InstinctResult with success=True
- `execute()` handles missing script gracefully
- `execute()` handles script timeout gracefully
- Cooldown is recorded after execution

## Task 9: Update pulse/config/pulse.yaml

Add instincts configuration section:
```yaml
instincts:
  enabled: true
  instincts_dir: "instincts"   # relative to pulse package root
  fire_before_generate: true   # check instincts before LLM synthesis
```

## Completion

After implementing all tasks:

1. Run: `cd /Users/iris/.openclaw/workspace/pulse && python -m pytest tests/unit/test_instincts/ -v 2>&1 | tail -20`
2. Run: `python -c "from pulse.src.instincts import InstinctRegistry; r = InstinctRegistry(Path('instincts')); print(r.all_instincts())"`
3. Commit all changes with message: `feat: Pulse Instincts system v0.5.0 — drive-triggered autonomous skills`
4. Notify: `openclaw system event --text "Done: Pulse v0.5.0 Instincts system built — drive-triggered autonomous skills, 3 initial instincts, wired into daemon GENERATE step" --mode now`
