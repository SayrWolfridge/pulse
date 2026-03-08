from pathlib import Path

from pulse.src.instincts.registry import InstinctRegistry


def _write_instinct(base: Path, name: str, enabled: bool = True):
    folder = base / name
    folder.mkdir(parents=True)
    folder.joinpath("INSTINCT.md").write_text(
        f"""---
name: {name}
description: test instinct
version: "1.0"
enabled: {str(enabled).lower()}
triggers:
  drives:
    curiosity: ">= 3.0"
  context:
    gfs_window: true
cooldown_minutes: 15
timeout_seconds: 30
output:
  log: true
script: run.py
---

Body text.
"""
    )
    folder.joinpath("run.py").write_text("print('ok')\n")


def test_registry_loads_instincts_from_directory(tmp_path):
    _write_instinct(tmp_path, "alpha")
    _write_instinct(tmp_path, "beta")

    registry = InstinctRegistry(tmp_path)

    assert [instinct.name for instinct in registry.all_instincts()] == ["alpha", "beta"]


def test_match_returns_instinct_when_drive_and_context_satisfy(tmp_path):
    _write_instinct(tmp_path, "alpha")
    registry = InstinctRegistry(tmp_path)

    matched = registry.match({"curiosity": 3.5}, {"gfs_window": True})

    assert [instinct.name for instinct in matched] == ["alpha"]


def test_match_returns_empty_when_drives_below_threshold(tmp_path):
    _write_instinct(tmp_path, "alpha")
    registry = InstinctRegistry(tmp_path)

    matched = registry.match({"curiosity": 2.9}, {"gfs_window": True})

    assert matched == []


def test_evaluate_condition_handles_all_supported_operators(tmp_path):
    _write_instinct(tmp_path, "alpha")
    registry = InstinctRegistry(tmp_path)

    assert registry._evaluate_condition(">= 3.0", 3.0) is True
    assert registry._evaluate_condition("<= 3.0", 3.0) is True
    assert registry._evaluate_condition("> 2", 3.0) is True
    assert registry._evaluate_condition("< 4", 3.0) is True
    assert registry._evaluate_condition("== 3", 3.0) is True
    assert registry._evaluate_condition("!= 4", 3.0) is True
    assert registry._evaluate_condition("bogus", 3.0) is False


def test_disabled_instincts_are_not_loaded_or_matched(tmp_path):
    _write_instinct(tmp_path, "alpha", enabled=False)
    registry = InstinctRegistry(tmp_path)

    assert registry.all_instincts() == []
    assert registry.match({"curiosity": 4.0}, {"gfs_window": True}) == []


def test_context_conditions_must_match(tmp_path):
    _write_instinct(tmp_path, "alpha")
    registry = InstinctRegistry(tmp_path)

    matched = registry.match({"curiosity": 4.0}, {"gfs_window": False})

    assert matched == []
