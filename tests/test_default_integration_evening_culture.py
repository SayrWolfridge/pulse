from pulse.src.core.config import PulseConfig
from pulse.src.drives.engine import Drive
from pulse.src.evaluator.priority import TriggerDecision
from pulse.src.integrations.default import DefaultIntegration


def _message_for(status: str) -> str:
    drive = Drive(
        name="evening_culture",
        category="evening_culture",
        pressure=0.9,
        weight=1.0,
    )
    drive.source_data["evening_culture"] = {
        "current_topic": "Иисус Навин и Иисус Христос",
        "current_status": status,
    }
    return DefaultIntegration().build_trigger_message(
        TriggerDecision(
            should_trigger=True,
            reason="single_drive_threshold: evening_culture",
            total_pressure=0.9,
            top_drive=drive,
        ),
        PulseConfig(),
    )


def test_evening_culture_prompt_is_short_visible_and_non_technical():
    message = _message_for("selected")

    assert "Тема: Иисус Навин и Иисус Христос" in message
    assert "Ход: первое приглашение" in message
    assert "никогда не отвечай NO_REPLY" in message
    assert "Trigger reason" not in message
    assert "pressure" not in message
    assert "Drive-specific protocol" not in message
    assert "skipped" not in message


def test_evening_culture_prompt_labels_existing_offer_as_reminder():
    message = _message_for("offered")

    assert "Ход: мягкое повторное приглашение" in message
