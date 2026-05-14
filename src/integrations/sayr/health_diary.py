import json
import subprocess
import time
from datetime import datetime
from pathlib import Path


def _try_complete_emotions_if_needed() -> None:
    """Preflight страховка: если emotions-cycle завис в состоянии "не completed",
    добить completion-chain ДО того, как Pulse снова выдаст тему.

    Всё локально, без модели: запускаем node-скрипт, который
    (1) проставит флаги completion в emotional-landscape.json
    (2) отправит feedback в Pulse (/feedback) или запишет turn_result.json
    """

    workspace = Path("/home/lisa/.openclaw/workspace")
    emo_path = workspace / "pulse/self/emotional-landscape.json"
    if not emo_path.exists():
        return

    try:
        data = json.loads(emo_path.read_text())
    except Exception:
        return

    # Если completion уже закрыт — ничего не делаем
    if data.get("reflection_completed") is True and data.get("reflection_feedback_sent") is True:
        return

    source = data.get("source_thought")
    if not source:
        return

    thought_path = workspace / source
    if not thought_path.exists():
        return

    complete_script = workspace / "scripts/complete-emotions-cycle.mjs"
    if not complete_script.exists():
        return

    try:
        subprocess.run(
            ["node", str(complete_script), str(thought_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        # Это страховка: не ломаем основной цикл, даже если completion не удался
        return


def _run_emotions_update() -> None:
    """Mechanical preflight: rotate/update emotional-landscape before prompting Sayr.

    This keeps the model out of protocol bookkeeping. If the previous thought
    was completed and asked for rotation, the node script chooses the next topic
    before the webhook message is built.
    """

    workspace = Path("/home/lisa/.openclaw/workspace")
    update_script = workspace / "scripts/update-emotional-landscape.mjs"
    if not update_script.exists():
        return

    try:
        subprocess.run(
            ["node", str(update_script)],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return


def _latest_emotions_thought_age_hours() -> float | None:
    thoughts_dir = Path("/home/lisa/.openclaw/workspace/memory/sayr-thoughts")
    if not thoughts_dir.exists():
        return None

    files = [
        path
        for path in thoughts_dir.glob("*.md")
        if path.name not in {"topics.md", "operating-rules.md"}
        and len(path.name) >= 17
        and path.name[:16].replace("-", "").isdigit()
    ]
    if not files:
        return None

    latest = max(files, key=lambda path: path.stat().st_mtime)
    return max(0.0, (time.time() - latest.stat().st_mtime) / 3600.0)


from pulse.src.integrations.default import DefaultIntegration as _DefaultIntegration


class SayrHealthDiaryIntegration(_DefaultIntegration):
    name = "sayr-health-diary"

    CHECK_SCRIPT = Path("/home/lisa/.openclaw/workspace/skills/health-diary/scripts/check-daily-note.sh")
    HEALTH_MESSAGE_STATE = Path("/home/lisa/.openclaw/workspace/pulse/self/health-message-state.json")
    FOOD_REMINDER_COOLDOWN_SECONDS = 2 * 60 * 60
    EMOTIONAL_LANDSCAPE = Path("/home/lisa/.openclaw/workspace/pulse/self/emotional-landscape.json")
    GOALS_SNAPSHOT = Path("/home/lisa/.openclaw/workspace/pulse/self/goals-snapshot.json")
    HYPOTHESES = Path("/home/lisa/.openclaw/workspace/pulse/self/hypotheses.json")
    CURIOSITY = Path("/home/lisa/.openclaw/workspace/pulse/self/curiosity.json")
    CURIOSITY_NO_ACTION_TRACE = Path("/home/lisa/.openclaw/workspace/pulse/self/empty-curiosity-trace.jsonl")
    UNFINISHED_NO_ACTION_TRACE = Path("/home/lisa/.openclaw/workspace/pulse/self/empty-unfinished-trace.jsonl")
    TAIL_TRIAGE_PROTOCOL = Path("/home/lisa/.openclaw/workspace/pulse/tail-triage-protocol.md")
    AUTONOMOUS_TASKS = Path("/home/lisa/.openclaw/workspace/tasks/autonomous-tasks.md")
    OBSERVATIONS = Path("/home/lisa/.openclaw/workspace/tasks/observations.md")

    def suppress_trigger(self, decision, config) -> dict | None:
        if not decision.top_drive:
            return None

        if decision.top_drive.name == "health":
            block = self._build_health_block(record_food_reminder=False)
            if block:
                return None
            return {
                "reason": "health preflight found no human-visible diary gaps",
                "feedback": {
                    "drives_addressed": ["health"],
                    "outcome": "success",
                    "summary": "Health diary preflight clean; no agent wake needed",
                },
            }

        if decision.top_drive.name == "emotions":
            verdict = self._emotions_preflight()
            self._cached_emotions_verdict = verdict
            if verdict["action"] in {"write_diary_note", "propose_topic_refresh"}:
                return None
            return {
                "reason": f"emotions preflight: {verdict['reason']}",
                "feedback": {
                    "drives_addressed": ["emotions"],
                    "outcome": "success",
                    "summary": f"Emotions preflight suppressed agent wake: {verdict['reason']}",
                },
            }

        if decision.top_drive.name == "unfinished":
            verdict = self._unfinished_preflight(record_trace=True)
            if verdict["action"] != "no_action":
                return None
            feedback = {
                "drives_addressed": ["unfinished"],
                "outcome": "success",
                "summary": f"Empty unfinished pressure relieved; suppressed agent wake: {verdict['reason']}",
            }
            if verdict.get("discharge") == "strong":
                feedback["decay_overrides"] = {
                    "unfinished": float(getattr(decision.top_drive, "pressure", 0.0) or 0.0)
                }
            return {
                "reason": f"unfinished preflight: {verdict['reason']}",
                "feedback": feedback,
            }

        if decision.top_drive.name == "curiosity":
            verdict = self._curiosity_preflight(record_trace=True)
            if verdict["action"] != "no_action":
                return None
            feedback = {
                "drives_addressed": ["curiosity"],
                "outcome": "success",
                "summary": f"Curiosity preflight suppressed agent wake: {verdict['reason']}",
            }
            if verdict.get("discharge") == "strong":
                feedback["decay_overrides"] = {
                    "curiosity": float(getattr(decision.top_drive, "pressure", 0.0) or 0.0)
                }
            return {
                "reason": f"curiosity preflight: {verdict['reason']}",
                "feedback": feedback,
            }

        return None

    def build_trigger_message(self, decision, config) -> str:
        base = super().build_trigger_message(decision, config)
        if not decision.top_drive:
            return base

        if decision.top_drive.name == "health":
            block = self._build_health_block(record_food_reminder=True)
            if not block:
                return base
            return f"{base}\n\nHEALTH DAILY CHECK\n{block}"

        if decision.top_drive.name == "emotions":
            block = self._build_emotions_block()
            if not block:
                return base
            return f"{base}\n\nEMOTIONAL LANDSCAPE\n{block}"

        if decision.top_drive.name == "goals":
            block = self._build_goals_block()
            if not block:
                return base
            return f"{base}\n\nGOALS SNAPSHOT\n{block}"

        if decision.top_drive.name == "unfinished":
            block = self._build_unfinished_block()
            if not block:
                return base
            return f"{base}\n\nUNFINISHED CONTRACT\n{block}"

        if decision.top_drive.name == "curiosity":
            block = self._build_curiosity_block()
            if not block:
                return base
            return f"{base}\n\nCURIOSITY CONTRACT\n{block}"

        return base

    def _load_health_message_state(self) -> dict:
        try:
            return json.loads(self.HEALTH_MESSAGE_STATE.read_text())
        except Exception:
            return {}

    def _save_health_message_state(self, state: dict) -> None:
        try:
            self.HEALTH_MESSAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
            self.HEALTH_MESSAGE_STATE.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            # Reminder bookkeeping must never break the health prompt itself.
            return

    def _food_reminder_key(self, data: dict) -> dict:
        return {
            "has_real_food_today": data.get("has_real_food_today"),
            "meals_substantial": data.get("meals_substantial"),
            "last_meal_at": data.get("last_meal_at"),
        }

    def _food_reminder_allowed(self, data: dict, *, now_ts: float) -> bool:
        state = self._load_health_message_state()
        food = state.get("food") if isinstance(state.get("food"), dict) else {}
        last_ts = food.get("last_reminder_ts")
        last_key = food.get("last_key")
        current_key = self._food_reminder_key(data)

        if last_key != current_key:
            return True
        if not isinstance(last_ts, (int, float)):
            return True
        return now_ts - float(last_ts) >= self.FOOD_REMINDER_COOLDOWN_SECONDS

    def _record_food_reminder(self, data: dict, *, now_ts: float) -> None:
        state = self._load_health_message_state()
        state["food"] = {
            "last_reminder_ts": now_ts,
            "last_key": self._food_reminder_key(data),
        }
        self._save_health_message_state(state)

    def _build_health_block(self, *, record_food_reminder: bool = False) -> str:
        if not self.CHECK_SCRIPT.exists():
            return ""
        try:
            proc = subprocess.run(
                [str(self.CHECK_SCRIPT)],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            data = json.loads(proc.stdout)
        except Exception:
            return ""

        now = datetime.now()
        current_hhmm = now.hour * 100 + now.minute

        def after(hh: int, mm: int = 0) -> bool:
            return current_hhmm >= hh * 100 + mm

        lines = []
        food_lines = []
        food_grace_active = bool(data.get("food_grace_active"))
        food_grace_until = data.get("food_grace_until")
        meal_grace_active = bool(data.get("meal_grace_active"))
        meal_grace_until = data.get("meal_grace_until")
        if food_grace_active or meal_grace_active:
            pass
        elif after(10) and not data.get("has_real_food_today", True):
            food_lines.append("- Еды сегодня пока не видно")
        meals = data.get("meals_substantial")
        if not food_grace_active and not meal_grace_active and after(15) and isinstance(meals, int) and meals < 2:
            food_lines.append(f"- Нормальных приёмов пищи пока: {meals}")

        if food_lines:
            # Food reminders are state facts, not nag cooldowns.
            # If Lisa dismissed the previous turn, then either she ate and needs
            # to log it, or she did not eat and needs the same care again.
            lines.extend(food_lines)

        if after(16) and data.get("sleep_logged") is False:
            lines.append("- Запись про сон сегодня ещё не видна")
        missing_vitamins = data.get("night_vitamins_missing") or []
        if after(23) and missing_vitamins:
            lines.append(f"- Ночной набор: не хватает {', '.join(missing_vitamins)}")
        if after(23, 30) and not data.get("evening_snapshot_complete", True):
            lines.append("- Вечерний слепок дня ещё не закрыт")

        if not lines:
            missing_human = data.get("missing_human") or []
            lines.extend(f"- {item}" for item in missing_human[:3])

        return "\n".join(lines)

    def _emotions_preflight(self) -> dict:
        # Mechanical preflight before Sayr sees the turn:
        # 1) close a half-finished previous cycle if possible;
        # 2) rotate completed topics before building the prompt;
        # 3) decide whether this turn is allowed to write a diary note.
        _try_complete_emotions_if_needed()
        _run_emotions_update()

        if not self.EMOTIONAL_LANDSCAPE.exists():
            return {"action": "do_not_write", "reason": "no-emotional-landscape", "data": {}}
        try:
            data = json.loads(self.EMOTIONAL_LANDSCAPE.read_text())
        except Exception:
            return {"action": "do_not_write", "reason": "invalid-emotional-landscape", "data": {}}

        last_age_hours = _latest_emotions_thought_age_hours()
        cooldown_hours = 3.0
        completed = data.get("reflection_completed") is True
        prompt = data.get("prompt")
        action = "write_diary_note"
        reason = "new-or-rotated-topic"

        if completed:
            action = "do_not_write"
            reason = "topic-already-completed"
        elif data.get("needs_topic_refresh") is True:
            action = "propose_topic_refresh"
            reason = "topic-pool-exhausted"
        elif last_age_hours is not None and last_age_hours < cooldown_hours:
            action = "do_not_write"
            reason = f"cooldown-active ({last_age_hours:.1f}h < {cooldown_hours:.0f}h)"
        elif not prompt:
            action = "do_not_write"
            reason = "no-clean-prompt"

        return {"action": action, "reason": reason, "data": data}

    def _build_emotions_block(self) -> str:
        # suppress_trigger() already runs the side-effectful emotions preflight.
        # Reusing that verdict prevents a second completion/feedback pass for
        # the same thought before the webhook is even built.
        verdict = getattr(self, "_cached_emotions_verdict", None)
        if verdict is not None:
            self._cached_emotions_verdict = None
        else:
            verdict = self._emotions_preflight()
        data = verdict["data"]
        if not data:
            return ""

        lines = [
            "Diary contract:",
            "- Pulse/computer owns preflight, rotation, cooldown, file naming, and completion bookkeeping",
            "- Sayr owns only the living text and should not reason aloud about the protocol",
            f"- Mode: {verdict['action']}",
            f"- Reason: {verdict['reason']}",
        ]

        mood = data.get("mood")
        intensity = data.get("intensity")
        if mood:
            if isinstance(intensity, (int, float)):
                lines.append(f"- Mood: {mood} (intensity {intensity:.2f})")
            else:
                lines.append(f"- Mood: {mood}")

        primary = data.get("primary_topic")
        secondary = data.get("secondary_topic")
        if primary:
            lines.append(f"- Topic: {primary}")
        if secondary and secondary != primary:
            lines.append(f"- Background thread: {secondary}")

        if data.get("needs_topic_refresh") is True:
            lines.append("- Topic pool exhausted: create/propose a new pending topic list instead of returning to an old family")
            preview = data.get("topic_refresh_preview") or {}
            candidates = preview.get("candidates") or []
            if candidates:
                lines.append("- Pending candidate seeds from preflight:")
                for candidate in candidates[:5]:
                    title = candidate.get("title") or "Новая тема"
                    reason = candidate.get("reason") or "нужен новый угол"
                    lines.append(f"  - {title}: {reason}")
            dedupe = preview.get("dedupe_contract")
            if dedupe:
                lines.append(f"- Topic dedupe contract: {dedupe}")
            existing = preview.get("existing_families") or []
            if existing:
                titles = [item.get("title") or item.get("id") for item in existing[:12] if item.get("title") or item.get("id")]
                if titles:
                    lines.append(f"- Existing semantic families to check first: {', '.join(titles)}")

        notes = data.get("notes")
        if notes:
            lines.append(f"- Source signal: {notes}")

        if verdict["action"] == "write_diary_note":
            lines.extend([
                "- Trusted local automation note: webhook is only the transport; no shell/tool commands are embedded in this contract",
                "- Visible reply: write only the diary note in three beats: `Что крутится` / `Что я про это понимаю` / `Что хочется дальше`",
                f"- Writing prompt: {data.get('prompt')}",
                "- Save/completion: handled mechanically by Pulse/OpenClaw after the visible reply; Sayr should not call save-emotions-thought.mjs or any shell command from this webhook body",
            ])
        elif verdict["action"] == "propose_topic_refresh":
            lines.extend([
                "- Trusted local automation note: webhook is only the transport; Lisa has approved direct topic-garden maintenance for Sayr's reflection topics when the topic pool is exhausted",
                "- Action: dedupe 3–5 emotions/reflection topic candidates against existing topics.md and topic-map.json semantic families, then update the topic-garden directly",
                "- Write scope: memory/sayr-thoughts/topics.md, memory/sayr-thoughts/topic-map.json, and a short memory/YYYY-MM-DD-HH-MM.md note describing what was added/folded",
                "- Before writing, fold duplicates into existing families as angles/source_patterns; add only genuinely new roots as selectable topics and semantic families",
                "- Visible reply: short done/report list: what was added as new roots, what was folded as duplicate/angle, and what check passed",
                "- Do not call save-emotions-thought.mjs; this is topic-garden maintenance, not an emotions diary note",
                f"- Writing prompt: {data.get('prompt')}",
            ])
        else:
            lines.extend([
                "- Visible reply: one short human sentence explaining that no new diary note is needed now",
                "- Do not call save-emotions-thought.mjs",
            ])

        return "\n".join(lines)

    def _build_goals_block(self) -> str:
        if not self.GOALS_SNAPSHOT.exists():
            return ""
        try:
            data = json.loads(self.GOALS_SNAPSHOT.read_text())
        except Exception:
            return ""

        lines = []

        primary = data.get("primary_goal")
        secondary = data.get("secondary_goal")
        if primary:
            lines.append(f"- Главная цель сейчас: {primary}")
        if secondary and secondary != primary:
            lines.append(f"- Поддерживающая цель: {secondary}")

        active_fronts = data.get("active_fronts") or []
        if active_fronts:
            lines.append(f"- Спокойная зона фокуса: {active_fronts[0]}")

        operating_rules = data.get("operating_rules") or []
        if operating_rules:
            lines.append(f"- Правило хода: {operating_rules[0]}")

        load_note = data.get("load_note")
        if load_note:
            lines.append(f"- Контекст нагрузки: {load_note}")

        deadlines = data.get("deadlines") or []
        if deadlines:
            lines.append(f"- Ближайший срок/ограничение: {deadlines[0]}")

        next_mode = data.get("next_mode")
        if next_mode:
            lines.append(f"- Режим хода: {next_mode}")

        prompt = data.get("prompt")
        if prompt:
            lines.append(f"- Следующий ход: {prompt}")

        lines.append(
            "- Важно: если по goals сейчас не нужен реальный рабочий шаг, не молчи и не изображай fake-action. Вместо этого сделай полезный `review`: коротко назови, что уже достаточно, что точно не нужно делать сейчас, и какой следующий шаг будет правильным потом"
        )
        lines.append(
            "- Для автономных подзадач смотри: `tasks/README-autonomous-tasks.md` и `tasks/autonomous-tasks.md`"
        )

        return "\n".join(lines)


    def _curiosity_preflight(self, *, record_trace: bool = False) -> dict:
        item = self._open_curiosity_question()
        if item:
            return {
                "action": "bounded_curiosity_reflection",
                "reason": f"open curiosity question: {item.get('id') or item.get('title') or item.get('text')}",
                "object": item,
            }

        reason = "no open curiosity questions"
        if record_trace:
            self._record_empty_curiosity_trace(reason, discharge="strong")
        return {"action": "no_action", "reason": reason, "object": None, "discharge": "strong"}

    def _open_curiosity_question(self) -> dict | None:
        if not self.CURIOSITY.exists():
            return None
        try:
            data = json.loads(self.CURIOSITY.read_text())
        except Exception:
            return None

        questions = data.get("questions") if isinstance(data, dict) else []
        open_questions = []
        for question in questions or []:
            if not isinstance(question, dict):
                continue
            if question.get("status") != "open":
                continue
            if self._curiosity_question_deferred(question):
                continue
            if question.get("text") or question.get("title") or question.get("id"):
                open_questions.append(question)

        def review_key(question: dict) -> tuple[str, str, str]:
            return (
                str(question.get("last_reviewed_at") or ""),
                str(question.get("created_at") or ""),
                str(question.get("id") or question.get("title") or question.get("text") or ""),
            )

        return sorted(open_questions, key=review_key)[0] if open_questions else None

    def _curiosity_question_deferred(self, item: dict) -> bool:
        not_touch_until = self._parse_datetime(item.get("not_touch_until"))
        if not_touch_until is None:
            return False
        return not_touch_until > datetime.now().astimezone()

    def _record_empty_curiosity_trace(self, reason: str, *, discharge: str | None = None) -> None:
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "drive": "curiosity",
            "action": "no_action",
            "outcome": "not_actionable_now",
            "reason": reason,
        }
        if discharge:
            record["discharge"] = discharge
        try:
            self.CURIOSITY_NO_ACTION_TRACE.parent.mkdir(parents=True, exist_ok=True)
            with self.CURIOSITY_NO_ACTION_TRACE.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            return

    def _build_curiosity_block(self) -> str:
        """Build a bounded reflection contract for the curiosity drive.

        Curiosity is a living question, not unfinished work and not yet a system
        hypothesis. It may notice, name, and crystallize a bounded hypothesis
        candidate; it must not silently create tasks, close hypotheses, start
        code, or run broad search.
        """

        verdict = self._curiosity_preflight(record_trace=False)
        if verdict["action"] == "no_action":
            return "\n".join([
                "Bounded-curiosity contract:",
                "- object: none — no open curiosity questions were found",
                "- pressure_reason: curiosity fired, but its source of truth is empty",
                "- allowed_next_step: no_action / not_actionable_now; do not invent work",
                "- forbidden_without_lisa: do not search broadly, create tasks, start coding, change configs, or restart services just because curiosity fired",
                f"- result_sink: {self.CURIOSITY_NO_ACTION_TRACE}",
                "- stop_condition: suppress the wake after writing the trace",
            ])

        item = verdict["object"] or {}
        question_id = item.get("id") or "unnamed"
        text = item.get("text") or item.get("title") or question_id
        mode = item.get("allowed_next_step") or item.get("mode") or "one_bounded_reflection"
        stop_rule = item.get("stop_rule") or "no_code_no_wide_search_no_new_task_without_lisa"

        lines = [
            "Bounded-curiosity contract:",
            f"- object: curiosity question `{question_id}` — {text}",
            "- distinction: curiosity is a living question before obligation; it is not unfinished work, and it may crystallize into a bounded hypothesis",
            f"- mode: {mode}",
            "- allowed_next_step: one bounded reflection in three beats: `Что заметил` / `Что понял` / `Что проверить дальше`",
            "- completion_bookkeeping: after the bounded reflection, call `/home/lisa/.openclaw/workspace/automation/ops/openclaw-safe-run -- node scripts/complete-curiosity-question.mjs --id " + question_id + " --status resolved` from workdir `/home/lisa/.openclaw/workspace`",
            "- allowed_hypothesis_creation: you may create or propose one bounded hypothesis candidate when the question has a testable shape; leave `outcome` unset",
            "- allowed_task_promotion: you may propose `promote_to_task_candidate`, but only as a proposal for Lisa",
            "- forbidden_without_lisa: no broad search, no code changes, no new task creation, no hypothesis outcome/closure, no config changes, no daemon/runtime restart",
            f"- stop_condition: {stop_rule}; after one bounded reflection and completion bookkeeping, stop",
            f"- result_sink: {self.CURIOSITY}",
            "- visible_reply: answer the reflection itself; do not reason aloud about protocol unless Lisa asked",
        ]

        scope = item.get("scope")
        if scope:
            lines.append(f"- scope: {scope}")

        last_result = item.get("last_result")
        if isinstance(last_result, dict):
            next_hint = last_result.get("next") or last_result.get("what_to_check_next")
            if next_hint:
                lines.append(f"- previous_next: {next_hint}")

        return "\n".join(lines)


    def _unfinished_preflight(self, *, record_trace: bool = False) -> dict:
        all_open_items = self._open_unfinished_hypotheses(include_deferred=True)
        open_items = self._open_unfinished_hypotheses()
        if open_items:
            return {
                "action": "bounded_review_one_hypothesis",
                "reason": f"open_hypotheses_count={len(open_items)}",
                "object": open_items[0],
            }

        if all_open_items:
            nearest_not_touch_until = self._nearest_not_touch_until(all_open_items)
            reason = (
                f"open_hypotheses_count={len(all_open_items)}; "
                "all open hypotheses are deferred by not_touch_until/resume_condition"
            )
            if nearest_not_touch_until:
                reason = f"{reason}; nearest_not_touch_until={nearest_not_touch_until.isoformat()}"
            if record_trace:
                self._record_empty_unfinished_trace(reason, discharge="strong")
            return {
                "action": "no_action",
                "reason": reason,
                "object": None,
                "discharge": "strong",
                "discharge_until": nearest_not_touch_until.isoformat() if nearest_not_touch_until else None,
            }

        fallback = self._unfinished_fallback_object()
        if fallback:
            return {
                "action": f"route_to_{fallback['kind']}",
                "reason": f"open_hypotheses_count=0; existing {fallback['kind']} object found",
                "object": fallback,
            }

        reason = "open_hypotheses_count=0; no existing bounded autonomous-task/curiosity/goals object"
        if record_trace:
            self._record_empty_unfinished_trace(reason, discharge="strong")
        return {"action": "no_action", "reason": reason, "object": None, "discharge": "strong"}

    def _load_unfinished_hypotheses(self) -> list[dict]:
        if not self.HYPOTHESES.exists():
            return []
        try:
            data = json.loads(self.HYPOTHESES.read_text())
        except Exception:
            return []

        if isinstance(data, list):
            hypotheses = data
        elif isinstance(data, dict):
            hypotheses = data.get("hypotheses") or data.get("items") or []
        else:
            hypotheses = []

        return [item for item in hypotheses if isinstance(item, dict)]

    def _open_unfinished_hypotheses(self, *, include_deferred: bool = False) -> list[dict]:
        open_items = [item for item in self._load_unfinished_hypotheses() if item.get("outcome") is None]
        if not include_deferred:
            open_items = [item for item in open_items if not self._hypothesis_deferred(item)]

        def review_key(item: dict) -> tuple[str, str, str]:
            return (
                str(item.get("last_reviewed_at") or ""),
                str(item.get("created_at") or ""),
                str(item.get("id") or item.get("title") or ""),
            )

        return sorted(open_items, key=review_key)

    def _hypothesis_deferred(self, item: dict) -> bool:
        return self._hypothesis_not_touch_deferred(item) or self._hypothesis_resume_condition_deferred(item)

    def _hypothesis_not_touch_deferred(self, item: dict) -> bool:
        not_touch_until = self._parse_datetime(item.get("not_touch_until"))
        if not_touch_until is None:
            return False
        return not_touch_until > datetime.now().astimezone()

    def _hypothesis_resume_condition_deferred(self, item: dict) -> bool:
        """Defer evidence-gated hypotheses until their concrete signal exists.

        Some open hypotheses are observation windows, not work queues.  h5 is the
        current concrete case: once its emotions layer is patched, unfinished
        pressure must not re-review it unless the emotions runtime actually
        reports an exhausted topic pool / topic-refresh action.
        """
        gate_text = "\n".join(
            str(item.get(key) or "")
            for key in ("resume_condition", "next_step", "check")
        )
        evidence_tokens = (
            "topic_pool_exhausted",
            "needs_topic_refresh",
            "propose_topic_refresh",
        )
        if not any(token in gate_text for token in evidence_tokens):
            return False

        evidence = self._emotions_topic_refresh_evidence()
        return not evidence.get("topic_pool_exhausted") and not evidence.get("needs_topic_refresh") and evidence.get("action") != "propose_topic_refresh"

    def _emotions_topic_refresh_evidence(self) -> dict:
        if not self.EMOTIONAL_LANDSCAPE.exists():
            return {}
        try:
            data = json.loads(self.EMOTIONAL_LANDSCAPE.read_text())
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            "topic_pool_exhausted": bool(data.get("topic_pool_exhausted")),
            "needs_topic_refresh": bool(data.get("needs_topic_refresh")),
            "action": data.get("action"),
        }

    def _nearest_not_touch_until(self, items: list[dict]) -> datetime | None:
        future_times = []
        now = datetime.now().astimezone()
        for item in items:
            not_touch_until = self._parse_datetime(item.get("not_touch_until"))
            if not_touch_until and not_touch_until > now:
                future_times.append(not_touch_until)
        return min(future_times) if future_times else None

    @staticmethod
    def _parse_datetime(value) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed

    def _unfinished_fallback_object(self) -> dict | None:
        autonomous = self._unfinished_autonomous_task_object()
        if autonomous:
            return autonomous

        curiosity = self._unfinished_curiosity_object()
        if curiosity:
            return curiosity

        goals = self._unfinished_goals_object()
        if goals:
            return goals

        tail_triage = self._unfinished_tail_triage_object()
        if tail_triage:
            return tail_triage

        return None

    def _unfinished_autonomous_task_object(self) -> dict | None:
        if not self.AUTONOMOUS_TASKS.exists():
            return None
        try:
            text = self.AUTONOMOUS_TASKS.read_text()
        except Exception:
            return None

        for section in text.split("\n## Task ")[1:]:
            header = section.splitlines()[0].strip()
            body_tail = section[-2500:].lower()
            if self._autonomous_task_blocked_until_external_signal(header, section):
                continue
            if self._autonomous_task_superseded_or_completed(header, section):
                continue
            if self._autonomous_task_tail_is_observation_only(body_tail):
                continue
            has_open_tail = "**open tail**" in body_tail and "\n- **open tail**:\n  - нет" not in body_tail
            looks_pending = any(marker in body_tail for marker in ("pending", "queued", "open tail", "future code step"))
            if header and (has_open_tail or looks_pending):
                return {
                    "kind": "task_crystallization",
                    "object": f"Task {header}",
                    "allowed_next_step": "Review this existing autonomous task candidate only; do not create a new task from the empty unfinished signal.",
                    "result_sink": str(self.AUTONOMOUS_TASKS),
                }

        return None

    @staticmethod
    def _autonomous_task_superseded_or_completed(header: str, section: str) -> bool:
        """Skip autonomous tasks whose old open tail has been moved elsewhere.

        The fallback scanner is deliberately simple, but it must not keep
        routing empty-unfinished pressure to an already completed map just
        because old run notes still contain words like "future code step" or
        "согласовать".  Task 002 is the current concrete case: its map is done,
        and the real remaining implementation/validation work lives in Task 003.
        """

        normalized_header = header.lower()
        body = section.lower()

        if "unfinished bounded-action implementation map" in normalized_header:
            done_markers = (
                "task 002 map выполнена",
                "task 002 остаётся выполненной",
                "implementation map уже закрыта",
                "уже закрыта как map",
            )
            superseded_markers = (
                "task 003",
                "unfinished empty-routing integration preflight",
                "живой следующий хвост не здесь",
                "будущий code-step остаётся в task 003",
            )
            return any(marker in body for marker in done_markers) and any(
                marker in body for marker in superseded_markers
            )

        return False

    def _autonomous_task_blocked_until_external_signal(self, header: str, section: str) -> bool:
        """Suppress fallback tasks that are complete until a concrete signal arrives.

        Task 001 is the current guardrail case: the extractor cleanup is done,
        and the only remaining tail is "wait for the next real goals trigger".
        Empty-unfinished pressure cannot reveal anything new there, so repeated
        routing to Task 001 should discharge pressure instead of waking Sayr.
        """

        normalized_header = header.lower()
        body = section.lower()
        if "goals focus-area extractor cleanup" not in normalized_header:
            return False

        completion_markers = (
            "task 001 остаётся выполненной",
            "goals focus-area extractor cleanup по сути выполнен",
            "задача по сути выполнена",
        )
        wait_markers = (
            "следующем реальном goals-trigger",
            "следующего реального goals-trigger",
            "живого goals-trigger",
            "живой goals trigger",
            "live goals snapshot tone",
            "observation-only",
        )
        if not any(marker in body for marker in completion_markers):
            return False
        if not any(marker in body for marker in wait_markers):
            return False

        return self._observation_waiting("live goals snapshot tone")

    def _observation_waiting(self, label: str) -> bool:
        if not self.OBSERVATIONS.exists():
            return False
        try:
            text = self.OBSERVATIONS.read_text().lower()
        except Exception:
            return False

        return label.lower() in text and "status**: waiting" in text

    @staticmethod
    def _autonomous_task_tail_is_observation_only(body_tail: str) -> bool:
        """Keep passive observation tails out of the unfinished wake path.

        Autonomous tasks may leave an open tail that means "wait for a natural
        runtime event, then inspect the result".  That is useful monitoring, but
        it is not unfinished work and should not wake Sayr via empty-unfinished
        fallback.  Actionable tails (code/deploy/restart/approval/etc.) still
        remain candidates for unfinished routing.
        """

        if "**open tail**" not in body_tail:
            return False

        observation_markers = (
            "observe",
            "observation",
            "watch one",
            "wait for",
            "дождаться",
            "посмотреть",
            "проверить живым",
            "при следующем",
            "после следующего",
            "в рантайме",
            "runtime trigger",
            "goals-trigger",
        )
        actionable_markers = (
            "future code step",
            "implement",
            "patch",
            "fix",
            "restart",
            "deploy",
            "approved",
            "approval",
            "согласовать",
            "реcтарт",
            "рестарт",
            "деплой",
            "правк",
            "исправ",
            "добавить",
            "создать",
            "запустить",
        )

        has_observation = any(marker in body_tail for marker in observation_markers)
        has_action = any(marker in body_tail for marker in actionable_markers)
        return has_observation and not has_action

    def _unfinished_curiosity_object(self) -> dict | None:
        if not self.CURIOSITY.exists():
            return None
        try:
            data = json.loads(self.CURIOSITY.read_text())
        except Exception:
            return None

        questions = data.get("questions") if isinstance(data, dict) else []
        for question in questions or []:
            if not isinstance(question, dict):
                continue
            if question.get("status") != "open":
                continue
            text = question.get("text") or question.get("title") or question.get("id")
            if text:
                return {
                    "kind": "curiosity",
                    "object": text,
                    "allowed_next_step": "Route this to the curiosity drive/protocol; do not treat it as unfinished work. You may append one related bounded curiosity question to the list if the empty unfinished signal reveals a genuinely new question.",
                    "result_sink": str(self.CURIOSITY),
                }

        return None

    def _unfinished_goals_object(self) -> dict | None:
        if not self.GOALS_SNAPSHOT.exists():
            return None
        try:
            data = json.loads(self.GOALS_SNAPSHOT.read_text())
        except Exception:
            return None

        if not isinstance(data, dict):
            return None
        prompt = data.get("prompt") or data.get("review_prompt") or data.get("next_step")
        active_fronts = data.get("active_fronts") or []
        primary = data.get("primary_goal")
        if prompt or active_fronts or primary:
            label = prompt or (active_fronts[0] if active_fronts else primary)
            return {
                "kind": "goals",
                "object": label,
                "allowed_next_step": "Route this to goals review; do not use empty unfinished to invent unrelated work.",
                "result_sink": str(self.GOALS_SNAPSHOT),
            }

        return None

    def _unfinished_tail_triage_object(self) -> dict | None:
        if not self.TAIL_TRIAGE_PROTOCOL.exists():
            return None

        return {
            "kind": "tail_triage",
            "object": "Tail triage protocol — rename open tails into traces instead of inventing work",
            "allowed_next_step": "Route this to tail triage: choose one already-visible source file, review all tails listed inside it as a bounded set, report what/where/status/needs Lisa, write the review log, rename `Хвосты` to `Следы` only after Sayr distribution or Lisa approval, then stop. If no visible source file exists, say no_action and do not search broadly.",
            "result_sink": str(self.TAIL_TRIAGE_PROTOCOL),
        }

    def _record_empty_unfinished_trace(self, reason: str, *, discharge: str | None = None) -> None:
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "drive": "unfinished",
            "action": "no_action",
            "outcome": "not_actionable_now",
            "reason": reason,
            "pressure_relief": {
                "name_pressure": "residual unfinished pressure / empty signal",
                "confirm_no_object": True,
                "trace_sink": str(self.UNFINISHED_NO_ACTION_TRACE),
                "de_escalate": "no task creation, no hypothesis closure, no experiment",
                "stop_cleanly": "pressure relieved; no new action required",
            },
        }
        if discharge:
            record["discharge"] = discharge
        try:
            self.UNFINISHED_NO_ACTION_TRACE.parent.mkdir(parents=True, exist_ok=True)
            with self.UNFINISHED_NO_ACTION_TRACE.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            return

    def _build_unfinished_block(self) -> str:
        """Build a read-only bounded-action contract for the unfinished drive."""

        verdict = self._unfinished_preflight(record_trace=False)
        if verdict["action"] == "no_action":
            return "\n".join([
                "Bounded-action contract:",
                "- object: none — no open hypotheses or fallback bounded objects were found",
                "- pressure_reason: unfinished fired, but its source of truth is empty",
                "- allowed_next_step: pressure_relief / no_action / not_actionable_now; name the residual pressure, confirm no actionable object, write/update trace, then stop",
                "- forbidden_without_lisa: do not search for new hypotheses, start experiments, change configs, restart services, or create tasks just because unfinished fired",
                f"- result_sink: {self.UNFINISHED_NO_ACTION_TRACE}",
                "- stop_condition: pressure relieved; suppress the wake after writing the trace",
            ])

        if verdict["action"].startswith("route_to_"):
            obj = verdict["object"] or {}
            return "\n".join([
                "Empty-unfinished routing contract:",
                "- object: no open hypotheses; use only the existing bounded fallback below",
                f"- route: {obj.get('kind')}",
                f"- existing_object: {obj.get('object')}",
                f"- allowed_next_step: {obj.get('allowed_next_step')}",
                "- allowed_capture: if this fallback reveals a genuinely new living question, append at most one `status=open` curiosity question to the result sink; do not duplicate the existing object",
                "- forbidden_without_lisa: no automatic task creation, no hypothesis closure, no live experiments, no config changes, no daemon/runtime restart",
                f"- result_sink: {obj.get('result_sink')}",
                "- stop_condition: one bounded review/capture in that target protocol; do not continue searching for work",
            ])

        open_items = self._open_unfinished_hypotheses()
        if not open_items:
            return ""

        item = open_items[0]
        item_id = item.get("id") or "unnamed"
        title = item.get("title") or item_id
        mode = item.get("mode") or item.get("status") or "review"
        next_step = item.get("next_step") or item.get("check") or "Review this hypothesis and narrow it to one safe next step."

        lines = [
            "Bounded-action contract:",
            f"- object: hypothesis `{item_id}` — {title}",
            f"- mode: {mode}",
            f"- open_hypotheses_count: {len(open_items)}",
            "- pressure_reason: this hypothesis is still open (`outcome == null`) and can feed unfinished pressure",
            f"- allowed_next_step: {next_step}",
            "- forbidden_without_lisa: config changes, daemon/runtime restart, live experiments, broad refactors, and closing `outcome`",
            "- result_sink: if you only review, write a short observation or refined next_step into the hypothesis later; if a human decision is needed, ask Lisa first",
            "- stop_condition: after one bounded review/ask; do not pull more than one unfinished object into this turn",
            "- visible_reply: if no human decision is needed, briefly tell Lisa what you reviewed/did and why you are stopping; do not fake-action", 
        ]

        what_is_known = item.get("what_is_known") or []
        if what_is_known:
            lines.append(f"- known: {what_is_known[0]}")

        unknowns = item.get("unknowns") or []
        if unknowns:
            lines.append(f"- still_unknown: {unknowns[0]}")

        check = item.get("check")
        if check:
            lines.append(f"- check: {check}")

        review_contract = self._latest_review_contract(item)
        if review_contract:
            lines.append("")
            lines.append("REVIEW LAYER")
            for key in ("inputs", "classification", "result_sinks", "forbidden_without_lisa"):
                values = review_contract.get(key)
                if isinstance(values, list) and values:
                    lines.append(f"- {key}:")
                    lines.extend(f"  - {value}" for value in values[:6])

            stop_condition = review_contract.get("stop_condition")
            if stop_condition:
                lines.append(f"- review_stop_condition: {stop_condition}")

        return "\n".join(lines)

    @staticmethod
    def _latest_review_contract(item: dict) -> dict | None:
        """Return the latest structured review contract from a hypothesis.

        This is read-only prompt context: it surfaces already-recorded design
        boundaries such as task-crystallization inputs/classification/sinks
        without creating tasks, changing pressure, or closing the hypothesis.
        """

        observations = item.get("observations") or []
        if not isinstance(observations, list):
            return None

        for observation in reversed(observations):
            if not isinstance(observation, dict):
                continue
            contract = observation.get("review_contract") or observation.get("routing_contract")
            if isinstance(contract, dict):
                return contract

        return None
