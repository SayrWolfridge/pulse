import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

from pulse.src.core.config import PulseConfig
from pulse.src.core.daemon import PulseDaemon
from pulse.src.drives.engine import Drive
from pulse.src.evaluator.priority import TriggerDecision
from pulse.src.integrations.sayr.git_pulse import (
    analyze_git_drive,
    execute_git_maintenance,
)
from pulse.src.integrations.sayr.health_diary import SayrHealthDiaryIntegration


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _decision(repo_path: str, repo_name: str = "workspace") -> TriggerDecision:
    drive = Drive(name=f"{repo_name}_git", category=f"{repo_name}_git", pressure=2.0, weight=1.0)
    drive.source_data["git"] = {
        "repo_name": repo_name,
        "repo_path": repo_path,
        "reasons": ["dirty_worktree"],
        "pressure_dirty": True,
        "stale_push": False,
        "commits_ahead": 0,
        "commits_behind": 0,
    }
    return TriggerDecision(
        should_trigger=True,
        reason=f"single_drive_threshold: {repo_name}_git",
        total_pressure=2.0,
        top_drive=drive,
    )


def test_analyze_git_drive_returns_commit_needed_with_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    action = analyze_git_drive(_decision(str(repo)))

    assert action is not None
    assert action.kind == "commit_needed"
    assert action.headline == "ЗАКОММИТЬ ВОТ ЭТО РЕПО"
    assert action.dirty_files == ["tracked.txt", "new.txt"]


def test_executor_fails_closed_for_directory_that_is_not_a_git_repo(tmp_path):
    repo = tmp_path / "not-a-repo"
    repo.mkdir()

    result = execute_git_maintenance(
        _decision(str(repo)), receipt_dir=tmp_path / "receipts",
    )

    assert result is not None
    assert result.outcome == "blocked"
    assert result.resolves_drive is False


def test_executor_commits_only_safe_workspace_additions(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("keep uncommitted\n", encoding="utf-8")
    (repo / "memory").mkdir()
    (repo / "memory" / "2026-08-30.md").write_text("new memory\n", encoding="utf-8")
    (repo / "memory" / "empty.md").write_text("", encoding="utf-8")
    (repo / "other.md").write_text("outside allowlist\n", encoding="utf-8")

    result = execute_git_maintenance(
        _decision(str(repo)), receipt_dir=tmp_path / "receipts",
    )

    assert result is not None
    assert result.outcome == "committed_partial"
    assert result.resolves_drive is False
    assert result.committed_files == ["memory/2026-08-30.md"]
    assert set(result.remaining_files) == {"tracked.txt", "memory/empty.md", "other.md"}
    assert _git(repo, "show", "--format=", "--name-only", "HEAD").stdout.strip() == "memory/2026-08-30.md"
    assert Path(result.receipt_path).exists()


def test_executor_commits_obsidian_markdown_and_resolves_clean_repo(tmp_path):
    repo = _repo(tmp_path)
    (repo / "Дневник").mkdir()
    (repo / "Дневник" / "мысль.md").write_text("текст\n", encoding="utf-8")

    result = execute_git_maintenance(
        _decision(str(repo), "obsidian"), receipt_dir=tmp_path / "receipts",
    )

    assert result is not None
    assert result.outcome == "committed"
    assert result.resolves_drive is True
    assert result.committed_files == ["Дневник/мысль.md"]
    assert _git(repo, "status", "--short").stdout == ""


def test_executor_preserves_trailing_spaces_and_accepts_ordinary_large_markdown(tmp_path):
    repo = _repo(tmp_path)
    (repo / "memory").mkdir()
    content = "Markdown hard break  \n" + ("x" * (600 * 1024)) + "\n"
    note = repo / "memory" / "large.md"
    note.write_text(content, encoding="utf-8")

    result = execute_git_maintenance(
        _decision(str(repo)), receipt_dir=tmp_path / "receipts",
    )

    assert result is not None
    assert result.outcome == "committed"
    assert result.committed_files == ["memory/large.md"]
    assert _git(repo, "show", "HEAD:memory/large.md").stdout == content


def test_executor_stages_topic_map_only_for_exact_note_companions(tmp_path):
    def prepare(root: Path, *, unrelated_change: bool) -> tuple[Path, list[str]]:
        root.mkdir()
        repo = _repo(root)
        garden = repo / "semantic-garden" / "sayr-thoughts"
        garden.mkdir(parents=True)
        note_paths = [
            "semantic-garden/sayr-thoughts/first.md",
            "semantic-garden/sayr-thoughts/second.md",
        ]
        families = [
            {
                "id": "first",
                "runtime_hint": "keep",
                "touch_count": 3,
                "last_touched_at": "2026-08-01T00:00:00Z",
            },
            {
                "id": "second",
                "runtime_hint": "keep",
                "touch_count": 7,
                "last_touched_at": "2026-08-02T00:00:00Z",
            },
        ]
        topic_map = garden / "topic-map.json"
        topic_map.write_text(
            json.dumps({"schema": "test", "families": families}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        _git(repo, "add", "semantic-garden/sayr-thoughts/topic-map.json")
        _git(repo, "commit", "-m", "add topic map")
        texts = ["first result", "second result"]
        for relative_path, text in zip(note_paths, texts, strict=True):
            (repo / relative_path).write_text(text + "\n", encoding="utf-8")
        updated = json.loads(topic_map.read_text(encoding="utf-8"))
        for index, family in enumerate(updated["families"]):
            stamp = f"2026-09-02T00:00:0{index}Z"
            family.update({
                "touch_count": family["touch_count"] + 1,
                "last_touched_at": stamp,
                "last_meaningful_result": texts[index],
                "last_result_source": note_paths[index],
                "last_result_at": stamp,
            })
        if unrelated_change:
            updated["families"][0]["runtime_hint"] = "changed"
        topic_map.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return repo, note_paths

    valid_repo, valid_notes = prepare(tmp_path / "valid", unrelated_change=False)
    valid = execute_git_maintenance(
        _decision(str(valid_repo)), receipt_dir=tmp_path / "valid-receipts",
    )

    assert valid is not None
    assert valid.outcome == "committed"
    assert valid.resolves_drive is True
    assert valid.committed_files == sorted([
        *valid_notes,
        "semantic-garden/sayr-thoughts/topic-map.json",
    ])
    assert _git(valid_repo, "status", "--short").stdout == ""

    invalid_repo, invalid_notes = prepare(tmp_path / "invalid", unrelated_change=True)
    invalid = execute_git_maintenance(
        _decision(str(invalid_repo)), receipt_dir=tmp_path / "invalid-receipts",
    )

    assert invalid is not None
    assert invalid.outcome == "committed_partial"
    assert invalid.resolves_drive is False
    assert invalid.committed_files == invalid_notes
    assert invalid.remaining_files == ["semantic-garden/sayr-thoughts/topic-map.json"]
    assert _git(invalid_repo, "diff", "--name-only").stdout.strip() == (
        "semantic-garden/sayr-thoughts/topic-map.json"
    )


def test_executor_fails_closed_when_index_is_not_empty(tmp_path):
    repo = _repo(tmp_path)
    (repo / "memory").mkdir()
    (repo / "memory" / "new.md").write_text("new\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    result = execute_git_maintenance(
        _decision(str(repo)), receipt_dir=tmp_path / "receipts",
    )

    assert result is not None
    assert result.outcome == "blocked"
    assert result.resolves_drive is False
    assert _git(repo, "diff", "--cached", "--name-only").stdout.strip() == "staged.txt"
    assert "memory/new.md" not in _git(repo, "diff", "--cached", "--name-only").stdout


def test_sayr_receives_result_only_after_deterministic_execution(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "memory").mkdir()
    (repo / "memory" / "new.md").write_text("new\n", encoding="utf-8")
    decision = _decision(str(repo))
    integration = SayrHealthDiaryIntegration()
    monkeypatch.setattr(
        "pulse.src.integrations.sayr.git_pulse.DEFAULT_RECEIPT_DIR",
        tmp_path / "receipts",
    )

    suppression = integration.suppress_trigger(decision, PulseConfig())
    message = integration.build_trigger_message(decision, PulseConfig())

    assert suppression is None
    assert "GIT RESULT" in message
    assert "deterministic_no_model" in message
    assert "GIT ACTION" not in message
    assert "do_not_run_git_or_spawn_subagents" in message
    assert decision.top_drive.source_data["git_maintenance_result"]["outcome"] == "committed"


def test_clean_repo_is_resolved_without_agent_wake(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    decision = _decision(str(repo))
    monkeypatch.setattr(
        "pulse.src.integrations.sayr.git_pulse.DEFAULT_RECEIPT_DIR",
        tmp_path / "receipts",
    )

    suppression = SayrHealthDiaryIntegration().suppress_trigger(decision, PulseConfig())

    assert suppression is not None
    assert suppression["reason"] == "git preflight: clean repo"
    assert suppression["feedback"]["drives_addressed"] == ["workspace_git"]


def test_http_202_does_not_decay_unresolved_git_drive(tmp_path):
    decision = _decision(str(_repo(tmp_path)))
    decision.top_drive.source_data["git_maintenance_result"] = {
        "outcome": "no_safe_slice",
        "resolves_drive": False,
    }
    daemon = PulseDaemon.__new__(PulseDaemon)
    daemon.drives = Mock()

    daemon._apply_trigger_drive_outcome(decision, delivery_success=True)

    daemon.drives.on_trigger_success.assert_not_called()
    daemon.drives.on_trigger_failure.assert_not_called()


def test_terminal_git_receipt_resolves_drive_even_if_delivery_fails(tmp_path):
    decision = _decision(str(_repo(tmp_path)))
    decision.top_drive.source_data["git_maintenance_result"] = {
        "outcome": "committed",
        "resolves_drive": True,
    }
    daemon = PulseDaemon.__new__(PulseDaemon)
    daemon.drives = Mock()

    daemon._apply_trigger_drive_outcome(decision, delivery_success=False)

    daemon.drives.on_trigger_success.assert_called_once_with(decision)
    daemon.drives.on_trigger_failure.assert_not_called()
