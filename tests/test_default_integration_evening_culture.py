from pulse.src.core.config import PulseConfig
from pulse.src.drives.engine import Drive
from pulse.src.evaluator.priority import TriggerDecision
from pulse.src.integrations.default import DefaultIntegration


def _message_for(status: str, *, started_discussing_at: str | None = None) -> str:
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
    if started_discussing_at:
        drive.source_data["evening_culture"]["started_discussing_at"] = started_discussing_at
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


def test_evening_culture_prompt_asks_status_for_started_topic():
    message = _message_for("offered", started_discussing_at="2026-07-15T18:57:00")

    assert "Ход: проверка статуса уже начатой темы" in message
    assert "не предлагай её как новую" in message
    assert "считать её закрытой или оставить открытой" in message
    assert "поменяй статус по её решению" in message
    assert "Не управляй вниманием Лисы" in message
    assert "Приди к Лисе с этой темой сейчас" not in message
