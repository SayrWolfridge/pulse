import json
import os
import subprocess
import time
from pathlib import Path

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
        """
        script_path = instinct.path / instinct.script
        fired_at = time.time()
        if not script_path.exists():
            self._cooldowns[instinct.name] = fired_at
            self._save()
            return InstinctResult(
                instinct_name=instinct.name,
                success=False,
                output="",
                error=f"Script not found: {script_path}",
                duration_seconds=0.0,
                fired_at=fired_at,
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
            error = None if success else output
        except subprocess.TimeoutExpired:
            duration = time.time() - start
            success = False
            output = f"Instinct timed out after {instinct.timeout_seconds}s"
            error = output
        except Exception as e:
            duration = time.time() - start
            success = False
            output = str(e)
            error = output

        self._cooldowns[instinct.name] = time.time()
        self._save()

        return InstinctResult(
            instinct_name=instinct.name,
            success=success,
            output=output,
            error=error,
            duration_seconds=duration,
            fired_at=self._cooldowns[instinct.name],
        )

    def _load(self) -> dict[str, float]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except Exception:
                return {}
        return {}

    def _save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self._cooldowns, indent=2))
