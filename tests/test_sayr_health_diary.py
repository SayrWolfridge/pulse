import json
import subprocess
from types import SimpleNamespace


class FixedDateTime:
    @classmethod
    def now(cls):
        from datetime import datetime

        return datetime(2026, 4, 30, 17, 30)


def _patch_health_check(monkeypatch, tmp_path, data):
    from pulse.src.integrations.sayr.health_diary import SayrHealthDiaryIntegration

    check_script = tmp_path / "check-daily-note.sh"
    check_script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(SayrHealthDiaryIntegration, "CHECK_SCRIPT", check_script)
    monkeypatch.setattr(
        SayrHealthDiaryIntegration,
        "HEALTH_MESSAGE_STATE",
        tmp_path / "health-message-state.json",
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(data),
            stderr="",
        )

    monkeypatch.setattr("pulse.src.integrations.sayr.health_diary.subprocess.run", fake_run)
    monkeypatch.setattr("pulse.src.integrations.sayr.health_diary.datetime", FixedDateTime)
    return SayrHealthDiaryIntegration()


def test_food_line_repeats_hourly_while_meals_are_still_missing(monkeypatch, tmp_path):
    data = {
        "has_real_food_today": True,
        "food_grace_active": False,
        "meal_grace_active": False,
        "meals_substantial": 1,
        "sleep_logged": False,
    }
    integration = _patch_health_check(monkeypatch, tmp_path, data)

    monkeypatch.setattr("pulse.src.integrations.sayr.health_diary.time.time", lambda: 1000.0)
    first = integration._build_health_block(record_food_reminder=True)
    assert "Нормальных приёмов пищи пока: 1" in first
    assert "Запись про сон сегодня ещё не видна" in first

    monkeypatch.setattr("pulse.src.integrations.sayr.health_diary.time.time", lambda: 1000.0 + 3600)
    second = integration._build_health_block(record_food_reminder=True)
    assert "Нормальных приёмов пищи пока: 1" in second
    assert "Еду уже поднимал недавно" not in second
    assert "Запись про сон сегодня ещё не видна" in second


def test_food_preflight_does_not_create_cooldown_state(monkeypatch, tmp_path):
    data = {
        "has_real_food_today": True,
        "food_grace_active": False,
        "meal_grace_active": False,
        "meals_substantial": 1,
        "sleep_logged": False,
    }
    integration = _patch_health_check(monkeypatch, tmp_path, data)

    monkeypatch.setattr("pulse.src.integrations.sayr.health_diary.time.time", lambda: 2000.0)
    preview = integration._build_health_block(record_food_reminder=False)
    assert "Нормальных приёмов пищи пока: 1" in preview
    assert not integration.HEALTH_MESSAGE_STATE.exists()


def test_food_state_change_is_reported_immediately(monkeypatch, tmp_path):
    data = {
        "has_real_food_today": False,
        "food_grace_active": False,
        "meal_grace_active": False,
        "meals_substantial": 0,
        "sleep_logged": False,
    }
    integration = _patch_health_check(monkeypatch, tmp_path, data)

    monkeypatch.setattr("pulse.src.integrations.sayr.health_diary.time.time", lambda: 3000.0)
    first = integration._build_health_block(record_food_reminder=True)
    assert "Еды сегодня пока не видно" in first

    data["has_real_food_today"] = True
    data["meals_substantial"] = 1
    monkeypatch.setattr("pulse.src.integrations.sayr.health_diary.time.time", lambda: 3000.0 + 600)
    changed = integration._build_health_block(record_food_reminder=True)
    assert "Нормальных приёмов пищи пока: 1" in changed
    assert "Еду уже поднимал недавно" not in changed


def test_recent_substantial_meal_suppresses_low_food_nudge(monkeypatch, tmp_path):
    data = {
        "has_real_food_today": True,
        "food_grace_active": False,
        "meal_grace_active": True,
        "meal_grace_until": "17:00",
        "meals_substantial": 1,
        "sleep_logged": True,
    }
    integration = _patch_health_check(monkeypatch, tmp_path, data)

    block = integration._build_health_block(record_food_reminder=True)
    assert "Нормальных приёмов пищи пока: 1" not in block
    assert block == ""


def test_emotions_build_reuses_suppress_preflight_verdict(monkeypatch):
    from pulse.src.integrations.sayr.health_diary import SayrHealthDiaryIntegration

    integration = SayrHealthDiaryIntegration()
    calls = {"count": 0}

    def fake_preflight():
        calls["count"] += 1
        return {
            "action": "write_diary_note",
            "reason": "new-or-rotated-topic",
            "data": {
                "mood": "warm",
                "intensity": 0.72,
                "primary_topic": "Fresh topic",
                "prompt": "Write fresh topic",
            },
        }

    monkeypatch.setattr(integration, "_emotions_preflight", fake_preflight)
    decision = SimpleNamespace(top_drive=SimpleNamespace(name="emotions", pressure=1.0))

    assert integration.suppress_trigger(decision, config=None) is None
    block = integration._build_emotions_block()

    assert calls["count"] == 1
    assert "Fresh topic" in block
    assert "Write fresh topic" in block


def test_emotions_write_contract_contains_no_shell_command(monkeypatch):
    from pulse.src.integrations.sayr.health_diary import SayrHealthDiaryIntegration

    integration = SayrHealthDiaryIntegration()

    monkeypatch.setattr(
        integration,
        "_emotions_preflight",
        lambda: {
            "action": "write_diary_note",
            "reason": "new-or-rotated-topic",
            "data": {
                "mood": "warm",
                "intensity": 0.72,
                "primary_topic": "Ясность как нежность",
                "prompt": "Напиши короткое размышление",
            },
        },
    )

    block = integration._build_emotions_block()

    assert "webhook is only the transport" in block
    assert "Visible reply: write only the diary note" in block
    assert "Save/completion: handled mechanically" in block
    assert "openclaw-safe-run" not in block
    assert "--text" not in block
    assert "call `/home/lisa" not in block


def _patch_unfinished_paths(monkeypatch, tmp_path, hypotheses):
    from pulse.src.integrations.sayr.health_diary import SayrHealthDiaryIntegration

    hypotheses_path = tmp_path / "hypotheses.json"
    hypotheses_path.write_text(json.dumps({"hypotheses": hypotheses}), encoding="utf-8")
    monkeypatch.setattr(SayrHealthDiaryIntegration, "HYPOTHESES", hypotheses_path)
    monkeypatch.setattr(SayrHealthDiaryIntegration, "CURIOSITY", tmp_path / "curiosity.json")
    monkeypatch.setattr(SayrHealthDiaryIntegration, "GOALS_SNAPSHOT", tmp_path / "goals-snapshot.json")
    monkeypatch.setattr(SayrHealthDiaryIntegration, "AUTONOMOUS_TASKS", tmp_path / "autonomous-tasks.md")
    monkeypatch.setattr(SayrHealthDiaryIntegration, "UNFINISHED_NO_ACTION_TRACE", tmp_path / "empty-unfinished-trace.jsonl")
    monkeypatch.setattr(SayrHealthDiaryIntegration, "TAIL_TRIAGE_PROTOCOL", tmp_path / "tail-triage-protocol.md")
    return SayrHealthDiaryIntegration()


def _patch_curiosity_paths(monkeypatch, tmp_path, questions):
    from pulse.src.integrations.sayr.health_diary import SayrHealthDiaryIntegration

    curiosity_path = tmp_path / "curiosity.json"
    curiosity_path.write_text(json.dumps({"questions": questions}), encoding="utf-8")
    monkeypatch.setattr(SayrHealthDiaryIntegration, "CURIOSITY", curiosity_path)
    monkeypatch.setattr(SayrHealthDiaryIntegration, "CURIOSITY_NO_ACTION_TRACE", tmp_path / "empty-curiosity-trace.jsonl")
    return SayrHealthDiaryIntegration()


def test_unfinished_open_hypotheses_route_to_bounded_review(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(
        monkeypatch,
        tmp_path,
        [
            {"id": "closed", "outcome": {"summary": "done"}},
            {"id": "h4", "title": "Empty unfinished routing", "outcome": None},
        ],
    )

    verdict = integration._unfinished_preflight(record_trace=True)
    assert verdict["action"] == "bounded_review_one_hypothesis"
    assert verdict["reason"] == "open_hypotheses_count=1"
    assert not integration.UNFINISHED_NO_ACTION_TRACE.exists()

    block = integration._build_unfinished_block()
    assert "hypothesis `h4`" in block
    assert "open_hypotheses_count: 1" in block


def test_unfinished_skips_hypotheses_deferred_by_not_touch_until(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "h2",
                "title": "Deferred emotions review",
                "outcome": None,
                "not_touch_until": "2999-05-05T14:25:45+03:00",
            },
            {
                "id": "h3",
                "title": "Available review",
                "outcome": None,
            },
        ],
    )

    verdict = integration._unfinished_preflight(record_trace=True)
    assert verdict["action"] == "bounded_review_one_hypothesis"
    assert verdict["reason"] == "open_hypotheses_count=1"
    assert verdict["object"]["id"] == "h3"
    assert not integration.UNFINISHED_NO_ACTION_TRACE.exists()


def test_unfinished_suppresses_when_all_hypotheses_deferred(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "h5",
                "title": "Deferred topic rotation observation",
                "outcome": None,
                "not_touch_until": "2999-05-05T13:40:00+03:00",
            }
        ],
    )

    verdict = integration._unfinished_preflight(record_trace=True)
    assert verdict["action"] == "no_action"
    assert "all open hypotheses are deferred by not_touch_until" in verdict["reason"]

    trace = integration.UNFINISHED_NO_ACTION_TRACE.read_text(encoding="utf-8")
    record = json.loads(trace.strip())
    assert record["action"] == "no_action"
    assert record["outcome"] == "not_actionable_now"
    assert "not_touch_until" in record["reason"]
    assert record["discharge"] == "strong"
    assert verdict["discharge"] == "strong"
    assert verdict["discharge_until"].startswith("2999-05-05T13:40:00")


def test_unfinished_defers_resume_condition_until_topic_refresh_evidence(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "h5",
                "title": "Topic rotation observation",
                "outcome": None,
                "next_step": "Wait for concrete emotions evidence only: topic_pool_exhausted/needs_topic_refresh becomes true or trigger/runtime block shows action=propose_topic_refresh.",
                "resume_condition": "Resume h5 only if concrete emotions evidence appears: topic_pool_exhausted/needs_topic_refresh true or action=propose_topic_refresh in runtime/trigger evidence.",
            }
        ],
    )
    landscape_path = tmp_path / "emotional-landscape.json"
    landscape_path.write_text(
        json.dumps({"topic_pool_exhausted": False, "needs_topic_refresh": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr(type(integration), "EMOTIONAL_LANDSCAPE", landscape_path)

    verdict = integration._unfinished_preflight(record_trace=True)

    assert verdict["action"] == "no_action"
    assert "resume_condition" in verdict["reason"]
    trace = integration.UNFINISHED_NO_ACTION_TRACE.read_text(encoding="utf-8")
    record = json.loads(trace.strip())
    assert record["action"] == "no_action"
    assert record["outcome"] == "not_actionable_now"
    assert record["discharge"] == "strong"


def test_unfinished_resume_condition_allows_topic_refresh_evidence(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "h5",
                "title": "Topic rotation observation",
                "outcome": None,
                "resume_condition": "Resume h5 only if concrete emotions evidence appears: topic_pool_exhausted/needs_topic_refresh true or action=propose_topic_refresh in runtime/trigger evidence.",
            }
        ],
    )
    landscape_path = tmp_path / "emotional-landscape.json"
    landscape_path.write_text(
        json.dumps({"topic_pool_exhausted": True, "needs_topic_refresh": True}),
        encoding="utf-8",
    )
    monkeypatch.setattr(type(integration), "EMOTIONAL_LANDSCAPE", landscape_path)

    verdict = integration._unfinished_preflight(record_trace=True)

    assert verdict["action"] == "bounded_review_one_hypothesis"
    assert verdict["object"]["id"] == "h5"


def test_unfinished_all_deferred_suppression_fully_discharges_drive(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "h5",
                "title": "Deferred topic rotation observation",
                "outcome": None,
                "not_touch_until": "2999-05-05T13:40:00+03:00",
            }
        ],
    )
    decision = SimpleNamespace(top_drive=SimpleNamespace(name="unfinished", pressure=1.37))

    suppression = integration.suppress_trigger(decision, config=None)

    assert suppression is not None
    assert suppression["feedback"]["drives_addressed"] == ["unfinished"]
    assert suppression["feedback"]["decay_overrides"] == {"unfinished": 1.37}


def test_empty_unfinished_without_object_requests_strong_discharge(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(monkeypatch, tmp_path, [])

    verdict = integration._unfinished_preflight(record_trace=True)

    assert verdict["action"] == "no_action"
    assert verdict["discharge"] == "strong"
    record = json.loads(integration.UNFINISHED_NO_ACTION_TRACE.read_text(encoding="utf-8").strip())
    assert record["discharge"] == "strong"
    assert record["pressure_relief"]["confirm_no_object"] is True
    assert record["pressure_relief"]["stop_cleanly"] == "pressure relieved; no new action required"


def test_empty_unfinished_routes_to_existing_curiosity_object(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(monkeypatch, tmp_path, [])
    integration.CURIOSITY.write_text(
        json.dumps({"questions": [{"id": "c1", "text": "Как держать хвосты мягко?", "status": "open"}]}),
        encoding="utf-8",
    )

    verdict = integration._unfinished_preflight(record_trace=True)
    assert verdict["action"] == "route_to_curiosity"
    assert "existing curiosity object found" in verdict["reason"]
    assert not integration.UNFINISHED_NO_ACTION_TRACE.exists()

    block = integration._build_unfinished_block()
    assert "Empty-unfinished routing contract" in block
    assert "route: curiosity" in block
    assert "Как держать хвосты мягко?" in block
    assert "allowed_capture" in block
    assert "append at most one `status=open` curiosity question" in block


def test_empty_unfinished_without_object_writes_no_action_trace(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(monkeypatch, tmp_path, [])

    verdict = integration._unfinished_preflight(record_trace=True)
    assert verdict["action"] == "no_action"
    assert "no existing bounded" in verdict["reason"]

    trace = integration.UNFINISHED_NO_ACTION_TRACE.read_text(encoding="utf-8")
    record = json.loads(trace.strip())
    assert record["action"] == "no_action"
    assert record["outcome"] == "not_actionable_now"
    assert record["drive"] == "unfinished"
    assert record["pressure_relief"]["name_pressure"] == "residual unfinished pressure / empty signal"


def test_empty_unfinished_no_action_contract_names_pressure_relief(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(monkeypatch, tmp_path, [])

    block = integration._build_unfinished_block()

    assert "pressure_relief" in block
    assert "name the residual pressure" in block
    assert "pressure relieved" in block


def test_empty_unfinished_suppression_summary_names_pressure_relief(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(monkeypatch, tmp_path, [])
    decision = SimpleNamespace(top_drive=SimpleNamespace(name="unfinished", pressure=0.46))

    suppression = integration.suppress_trigger(decision, config=None)

    assert suppression is not None
    assert "pressure relieved" in suppression["feedback"]["summary"]


def test_empty_unfinished_skips_observation_only_autonomous_tail(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(monkeypatch, tmp_path, [])
    integration.AUTONOMOUS_TASKS.write_text(
        """
# Autonomous tasks

## Task 001 — goals focus-area extractor cleanup
### Runs
#### Run 2026-05-11 13:35
- **Result**:
  - задача по сути выполнена
- **Open tail**:
  - при следующем реальном goals-trigger посмотреть, не возвращается ли сырой task/system backlog в пользовательскую формулировку
- **Question**:
  - нет
""".strip(),
        encoding="utf-8",
    )

    verdict = integration._unfinished_preflight(record_trace=True)

    assert verdict["action"] == "no_action"
    assert "no existing bounded" in verdict["reason"]
    record = json.loads(integration.UNFINISHED_NO_ACTION_TRACE.read_text(encoding="utf-8").strip())
    assert record["drive"] == "unfinished"
    assert record["discharge"] == "strong"


def test_empty_unfinished_keeps_actionable_autonomous_tail(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(monkeypatch, tmp_path, [])
    integration.AUTONOMOUS_TASKS.write_text(
        """
# Autonomous tasks

## Task 003 — unfinished empty-routing integration preflight
### Runs
#### Run 2026-05-04 09:50
- **Open tail**:
  - runtime still needs separately approved Pulse daemon restart/deploy before this affects live wakes
""".strip(),
        encoding="utf-8",
    )

    verdict = integration._unfinished_preflight(record_trace=True)

    assert verdict["action"] == "route_to_task_crystallization"
    assert "Task 003" in verdict["object"]["object"]
    assert not integration.UNFINISHED_NO_ACTION_TRACE.exists()


def test_empty_unfinished_routes_to_tail_triage_protocol(monkeypatch, tmp_path):
    integration = _patch_unfinished_paths(monkeypatch, tmp_path, [])
    integration.TAIL_TRIAGE_PROTOCOL.write_text("# Tail triage\n", encoding="utf-8")

    verdict = integration._unfinished_preflight(record_trace=True)

    assert verdict["action"] == "route_to_tail_triage"
    assert "existing tail_triage object found" in verdict["reason"]
    assert not integration.UNFINISHED_NO_ACTION_TRACE.exists()

    block = integration._build_unfinished_block()
    assert "Empty-unfinished routing contract" in block
    assert "route: tail_triage" in block
    assert "rename open tails into traces" in block
    assert str(integration.TAIL_TRIAGE_PROTOCOL) in block


def test_curiosity_open_question_adds_bounded_contract(monkeypatch, tmp_path):
    integration = _patch_curiosity_paths(
        monkeypatch,
        tmp_path,
        [
            {
                "id": "c1",
                "text": "Как мягко встроить Pulse в жизнь Лисы?",
                "status": "open",
                "scope": "relationship/system rhythm",
            }
        ],
    )

    verdict = integration._curiosity_preflight(record_trace=True)
    assert verdict["action"] == "bounded_curiosity_reflection"
    assert verdict["object"]["id"] == "c1"
    assert not integration.CURIOSITY_NO_ACTION_TRACE.exists()

    block = integration._build_curiosity_block()
    assert "Bounded-curiosity contract" in block
    assert "curiosity question `c1`" in block
    assert "not unfinished work" in block
    assert "may crystallize into a bounded hypothesis" in block
    assert "Что заметил" in block
    assert "completion_bookkeeping" in block
    assert "complete-curiosity-question.mjs --id c1 --status resolved" in block
    assert "relationship/system rhythm" in block


def test_curiosity_skips_resolved_questions(monkeypatch, tmp_path):
    integration = _patch_curiosity_paths(
        monkeypatch,
        tmp_path,
        [
            {"id": "c1", "text": "Already settled", "status": "resolved", "last_reviewed_at": "2026-05-05T09:34:00Z"},
            {"id": "c2", "text": "Still open", "status": "open"},
        ],
    )

    verdict = integration._curiosity_preflight(record_trace=True)

    assert verdict["action"] == "bounded_curiosity_reflection"
    assert verdict["object"]["id"] == "c2"
    assert not integration.CURIOSITY_NO_ACTION_TRACE.exists()


def test_curiosity_no_open_questions_suppresses_and_discharges(monkeypatch, tmp_path):
    integration = _patch_curiosity_paths(
        monkeypatch,
        tmp_path,
        [{"id": "c1", "text": "Closed", "status": "closed"}],
    )
    decision = SimpleNamespace(top_drive=SimpleNamespace(name="curiosity", pressure=1.23))

    suppression = integration.suppress_trigger(decision, config=None)

    assert suppression is not None
    assert suppression["feedback"]["drives_addressed"] == ["curiosity"]
    assert suppression["feedback"]["decay_overrides"] == {"curiosity": 1.23}

    record = json.loads(integration.CURIOSITY_NO_ACTION_TRACE.read_text(encoding="utf-8").strip())
    assert record["drive"] == "curiosity"
    assert record["action"] == "no_action"
    assert record["discharge"] == "strong"


def test_curiosity_contract_allows_bounded_hypothesis_creation_but_forbids_task_creation(monkeypatch, tmp_path):
    integration = _patch_curiosity_paths(
        monkeypatch,
        tmp_path,
        [{"id": "c2", "text": "Какой должна быть живая рабочая память Сэйра?", "status": "open"}],
    )

    block = integration._build_curiosity_block()

    assert "allowed_hypothesis_creation" in block
    assert "one bounded hypothesis candidate" in block
    assert "outcome` unset" in block
    assert "allowed_task_promotion" in block
    assert "no broad search" in block
    assert "no code changes" in block
    assert "no new task creation" in block
    assert "no hypothesis outcome/closure" in block
