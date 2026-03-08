import json
import time
from pathlib import Path

from pulse.src.instincts.executor import InstinctExecutor
from pulse.src.instincts.models import Instinct, InstinctOutput, InstinctTrigger


def _instinct(tmp_path: Path, script_name: str, cooldown_minutes: int = 5, timeout_seconds: int = 1) -> Instinct:
    return Instinct(
        name="test-instinct",
        description="test",
        version="1.0",
        enabled=True,
        triggers=InstinctTrigger(drives={"curiosity": ">= 1.0"}, context={}),
        cooldown_minutes=cooldown_minutes,
        timeout_seconds=timeout_seconds,
        output=InstinctOutput(),
        script=script_name,
        body="body",
        path=tmp_path,
    )


def test_is_ready_returns_true_when_cooldown_not_set(tmp_path):
    executor = InstinctExecutor(state_file=tmp_path / "cooldowns.json")

    assert executor.is_ready(_instinct(tmp_path, "run.py")) is True


def test_is_ready_returns_false_within_cooldown_window(tmp_path):
    state_file = tmp_path / "cooldowns.json"
    instinct = _instinct(tmp_path, "run.py", cooldown_minutes=10)
    state_file.write_text(json.dumps({instinct.name: time.time()}))
    executor = InstinctExecutor(state_file=state_file)

    assert executor.is_ready(instinct) is False


def test_is_ready_returns_true_when_cooldown_elapsed(tmp_path):
    state_file = tmp_path / "cooldowns.json"
    instinct = _instinct(tmp_path, "run.py", cooldown_minutes=1)
    state_file.write_text(json.dumps({instinct.name: time.time() - 120}))
    executor = InstinctExecutor(state_file=state_file)

    assert executor.is_ready(instinct) is True


def test_execute_runs_script_successfully(tmp_path):
    script = tmp_path / "run.py"
    script.write_text("print('ran instinct')\n")
    executor = InstinctExecutor(state_file=tmp_path / "cooldowns.json")

    result = executor.execute(_instinct(tmp_path, script.name), {"gfs_window": True})

    assert result.success is True
    assert "ran instinct" in result.output
    assert result.error is None


def test_execute_handles_missing_script(tmp_path):
    executor = InstinctExecutor(state_file=tmp_path / "cooldowns.json")

    result = executor.execute(_instinct(tmp_path, "missing.py"), {})

    assert result.success is False
    assert "Script not found" in result.error


def test_execute_handles_script_timeout(tmp_path):
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(2)\n")
    executor = InstinctExecutor(state_file=tmp_path / "cooldowns.json")

    result = executor.execute(
        _instinct(tmp_path, script.name, timeout_seconds=1),
        {},
    )

    assert result.success is False
    assert "timed out" in result.error


def test_execute_records_cooldown_after_execution(tmp_path):
    script = tmp_path / "run.py"
    script.write_text("print('done')\n")
    state_file = tmp_path / "cooldowns.json"
    executor = InstinctExecutor(state_file=state_file)
    instinct = _instinct(tmp_path, script.name)

    executor.execute(instinct, {})

    data = json.loads(state_file.read_text())
    assert instinct.name in data
