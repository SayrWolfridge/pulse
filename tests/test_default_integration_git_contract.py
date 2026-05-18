from pulse.src.drives.engine import Drive
from pulse.src.evaluator.priority import TriggerDecision
from pulse.src.integrations.default import DefaultIntegration
from pulse.src.core.config import PulseConfig


def test_default_trigger_message_includes_git_repo_contract():
    drive = Drive(name="pulse_git", category="pulse_git", pressure=2.0, weight=1.0)
    drive.source_data["git"] = {
        "repo_name": "pulse",
        "repo_path": "/home/lisa/addons/pulse",
        "reasons": ["stale_push"],
        "pressure_dirty": False,
        "stale_push": True,
        "commits_ahead": 16,
        "commits_behind": 0,
    }
    msg = DefaultIntegration().build_trigger_message(
        TriggerDecision(
            should_trigger=True,
            reason="single_drive_threshold: pulse_git",
            total_pressure=2.0,
            top_drive=drive,
        ),
        PulseConfig(),
    )

    assert "Git repo contract:" in msg
    assert "- repo_path: /home/lisa/addons/pulse" in msg
    assert "- drive: pulse_git" in msg
    assert "- reason: stale_push" in msg
    assert "Use repo_path for git checks" in msg
