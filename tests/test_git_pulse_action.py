from pulse.src.drives.engine import Drive
from pulse.src.evaluator.priority import TriggerDecision
from pulse.src.integrations.sayr.git_pulse import analyze_git_drive
from pulse.src.integrations.sayr.health_diary import SayrHealthDiaryIntegration
from pulse.src.core.config import PulseConfig


def _decision(repo_path: str):
    drive = Drive(name="pulse_git", category="pulse_git", pressure=2.0, weight=1.0)
    drive.source_data["git"] = {
        "repo_name": "pulse",
        "repo_path": repo_path,
        "reasons": ["dirty_worktree"],
        "pressure_dirty": True,
        "stale_push": False,
        "commits_ahead": 0,
        "commits_behind": 0,
    }
    return TriggerDecision(
        should_trigger=True,
        reason="single_drive_threshold: pulse_git",
        total_pressure=2.0,
        top_drive=drive,
    )


def test_analyze_git_drive_returns_commit_needed_with_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "changed.txt").write_text("hello\n")

    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "changed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "changed.txt").write_text("hello again\n")
    (repo / "new.txt").write_text("new\n")

    action = analyze_git_drive(_decision(str(repo)))

    assert action is not None
    assert action.kind == "commit_needed"
    assert action.headline == "НАДО ЗАКОММИТИТЬ ВОТ ЭТО"
    assert action.repo_path == str(repo)
    assert action.dirty_files == ["changed.txt", "new.txt"]


def test_sayr_git_trigger_message_includes_action_block(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "new.txt").write_text("new\n")

    msg = SayrHealthDiaryIntegration().build_trigger_message(_decision(str(repo)), PulseConfig())

    assert "GIT ACTION" in msg
    assert "НАДО ЗАКОММИТИТЬ ВОТ ЭТО" in msg
    assert f"- repo_path: {repo}" in msg
    assert "  - new.txt" in msg


def test_sayr_git_clean_preflight_suppresses_agent_wake(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("ok\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)

    suppression = SayrHealthDiaryIntegration().suppress_trigger(_decision(str(repo)), PulseConfig())

    assert suppression is not None
    assert suppression["reason"] == "git preflight: clean repo"
    assert suppression["feedback"]["drives_addressed"] == ["pulse_git"]
