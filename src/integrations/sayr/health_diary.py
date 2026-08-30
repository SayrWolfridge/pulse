import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from pulse.src.conversation_lifecycle import growth_callback_kind
from pulse.src.integrations.sayr.git_pulse import analyze_git_drive


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
    thoughts_dir = Path("/home/lisa/.openclaw/workspace/semantic-garden/sayr-thoughts")
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
    HEALTH_REMINDER_COOLDOWN_SECONDS = 2 * 60 * 60
    FOOD_REMINDER_COOLDOWN_SECONDS = HEALTH_REMINDER_COOLDOWN_SECONDS
    HEALTH_DRIVE_KINDS = {
        "health_food": "food",
        "health_sleep": "sleep",
        "health_vitamins": "night_vitamins",
        "health_snapshot": "evening_snapshot",
    }
    EMOTIONAL_LANDSCAPE = Path("/home/lisa/.openclaw/workspace/pulse/self/emotional-landscape.json")
    GOALS_SNAPSHOT = Path("/home/lisa/.openclaw/workspace/pulse/self/goals-snapshot.json")
    HYPOTHESES = Path("/home/lisa/.openclaw/workspace/pulse/self/hypotheses.json")
    CURIOSITY = Path("/home/lisa/.openclaw/workspace/pulse/self/curiosity.json")
    CURIOSITY_NO_ACTION_TRACE = Path("/home/lisa/.openclaw/workspace/pulse/self/empty-curiosity-trace.jsonl")
    SAYR_THOUGHTS_PROTOCOL = Path("/home/lisa/.openclaw/workspace/pulse/sayr-thoughts-consolidation-protocol.md")
    SAYR_THOUGHTS_INDEX = Path("/home/lisa/.openclaw/workspace/semantic-garden/sayr-thoughts/blog/INDEX.md")
    SAYR_THOUGHTS_PROCESS = Path("/home/lisa/.openclaw/workspace/semantic-garden/sayr-thoughts/blog/PROCESS.md")
    UNFINISHED_NO_ACTION_TRACE = Path("/home/lisa/.openclaw/workspace/pulse/self/empty-unfinished-trace.jsonl")
    TAIL_TRIAGE_PROTOCOL = Path("/home/lisa/.openclaw/workspace/pulse/tail-triage-protocol.md")
    AUTONOMOUS_TASKS = Path("/home/lisa/.openclaw/workspace/tasks/autonomous-tasks.md")
    OBSERVATIONS = Path("/home/lisa/.openclaw/workspace/tasks/observations.md")

    @classmethod
    def _health_kind_for_drive(cls, drive_name: str) -> str | None:
        if drive_name == "health":
            return None
        return cls.HEALTH_DRIVE_KINDS.get(drive_name)

    @classmethod
    def _is_health_drive(cls, drive_name: str) -> bool:
        return drive_name == "health" or drive_name in cls.HEALTH_DRIVE_KINDS

    def suppress_trigger(self, decision, config) -> dict | None:
        if not decision.top_drive:
            return None

        if self._is_health_drive(decision.top_drive.name):
            block = self._build_health_block(
                record_food_reminder=False,
                only_kind=self._health_kind_for_drive(decision.top_drive.name),
            )
            if block:
                return None
            return {
                "reason": "health preflight found no human-visible diary gaps",
                "feedback": {
                    "drives_addressed": [decision.top_drive.name],
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
            if verdict["action"].startswith("handoff_to_"):
                return self._suppress_unfinished_handoff(decision, verdict)
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

        git_action = analyze_git_drive(decision)
        if git_action and git_action.kind == "clean":
            return {
                "reason": "git preflight: clean repo",
                "feedback": {
                    "drives_addressed": [decision.top_drive.name],
                    "outcome": "success",
                    "summary": git_action.headline,
                    "decay_overrides": {
                        decision.top_drive.name: float(getattr(decision.top_drive, "pressure", 0.0) or 0.0)
                    },
                },
            }

        return None

    def _build_trigger_header_without_drive_protocol(self, decision, config) -> str:
        prefix = config.openclaw.message_prefix
        parts = [
            f"{prefix} Self-initiated turn.",
            f"Trigger reason: {decision.reason}",
        ]
        if decision.top_drive:
            parts.append(
                f"Top drive: {decision.top_drive.name} "
                f"(pressure: {decision.top_drive_pressure_snapshot:.2f})"
            )
        else:
            parts.append(f"Total pressure: {decision.total_pressure:.2f}")
        return "\n".join(parts)

    def build_trigger_message(self, decision, config) -> str:
        if decision.top_drive and decision.top_drive.name == "emotions":
            block = self._build_emotions_block()
            if block:
                base = self._build_trigger_header_without_drive_protocol(decision, config)
                return f"{base}\n\nEMOTIONAL LANDSCAPE\n{block}"

        if decision.top_drive and decision.top_drive.name == "curiosity":
            block = self._build_curiosity_block()
            if block:
                base = self._build_trigger_header_without_drive_protocol(decision, config)
                return f"{base}\n\nCURIOSITY CONTRACT\n{block}"

        if decision.top_drive and decision.top_drive.name == "growth":
            block = self._build_growth_conversation_block(decision.top_drive)
            if block:
                base = self._build_trigger_header_without_drive_protocol(decision, config)
                return f"{base}\n\nGROWTH CONVERSATION\n{block}"

        base = super().build_trigger_message(decision, config)
        if not decision.top_drive:
            return base

        if self._is_health_drive(decision.top_drive.name):
            block = self._build_health_block(
                record_food_reminder=True,
                only_kind=self._health_kind_for_drive(decision.top_drive.name),
            )
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

        git_action = analyze_git_drive(decision)
        if git_action:
            return f"{base}\n\n{git_action.as_message()}"

        return base

    def _build_growth_conversation_block(self, drive) -> str | None:
        candidate = drive.source_data.get("growth_material") or {}
        item_id = str(candidate.get("id") or "")
        if not item_id:
            return None
        try:
            callback_kind = growth_callback_kind(item_id)
        except ValueError:
            return None

        title = candidate.get("title") or item_id
        kind = candidate.get("kind") or "candidate"
        notes = candidate.get("notes") or ""
        suggested_home = candidate.get("suggested_home") or "unspecified"
        question = candidate.get("question") or (
            "Эта грань уже стала частью нашей общей формы, её лучше оставить на потом "
            "или не закреплять?"
        )
        return "\n".join([
            "Conversation-item contract:",
            f"- item: growth:{item_id}",
            f"- title: {title}",
            f"- kind: {kind}",
            f"- context: {notes}",
            f"- suggested_home: {suggested_home}",
            f"- one_question_for_lisa: {question}",
            "- visible_reply: write 2–4 living sentences about what you noticed, then ask exactly this one question; never answer NO_REPLY or HEARTBEAT_OK",
            "- lifecycle: do not call this discussed, accepted or offered yourself; Pulse marks awaiting_lisa only after the terminal visible-output callback",
            "- queue_boundary: do not bring another growth/garden decision while this item awaits Lisa",
            "- after_lisa_replies: use the pulse-feedback lifecycle helper for this exact item; quality feedback remains a separate action",
            f"PULSE_CONVERSATION_CALLBACK_KIND={callback_kind}",
        ])

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

    def _health_reminder_key(self, kind: str, data: dict, extra: dict | None = None) -> dict:
        key = {
            "kind": kind,
            "date": data.get("date"),
        }
        if extra:
            key.update(extra)
        return key

    def _health_reminder_allowed(self, kind: str, key: dict, *, now_ts: float) -> bool:
        state = self._load_health_message_state()
        item = state.get(kind) if isinstance(state.get(kind), dict) else {}
        last_ts = item.get("last_reminder_ts")
        last_key = item.get("last_key")

        if last_key != key:
            return True
        if not isinstance(last_ts, (int, float)):
            return True
        return now_ts - float(last_ts) >= self.HEALTH_REMINDER_COOLDOWN_SECONDS

    def _record_health_reminder(self, kind: str, key: dict, *, now_ts: float) -> None:
        state = self._load_health_message_state()
        state[kind] = {
            "last_reminder_ts": now_ts,
            "last_key": key,
        }
        self._save_health_message_state(state)

    def _food_reminder_key(self, data: dict) -> dict:
        return {
            "kind": "food",
            "date": data.get("date"),
            "has_real_food_today": data.get("has_real_food_today"),
            "meals_substantial": data.get("meals_substantial"),
            "last_meal_at": data.get("last_meal_at"),
        }

    def _food_reminder_allowed(self, data: dict, *, now_ts: float) -> bool:
        return self._health_reminder_allowed(
            "food",
            self._food_reminder_key(data),
            now_ts=now_ts,
        )

    def _record_food_reminder(self, data: dict, *, now_ts: float) -> None:
        self._record_health_reminder(
            "food",
            self._food_reminder_key(data),
            now_ts=now_ts,
        )

    def _maybe_add_health_line(
        self,
        lines: list[str],
        *,
        kind: str,
        key: dict,
        line: str,
        now_ts: float,
        record: bool,
    ) -> None:
        if not self._health_reminder_allowed(kind, key, now_ts=now_ts):
            return
        lines.append(line)
        if record:
            self._record_health_reminder(kind, key, now_ts=now_ts)

    def _build_health_block(
        self,
        *,
        record_food_reminder: bool = False,
        only_kind: str | None = None,
    ) -> str:
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
        now_ts = time.time()
        current_hhmm = now.hour * 100 + now.minute

        def after(hh: int, mm: int = 0) -> bool:
            return current_hhmm >= hh * 100 + mm

        def minutes_since_hhmm(value: object) -> int | None:
            if not isinstance(value, str):
                return None
            match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
            if not match:
                return None
            hour = int(match.group(1))
            minute = int(match.group(2))
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                return None
            event_minutes = hour * 60 + minute
            now_minutes = now.hour * 60 + now.minute
            delta = now_minutes - event_minutes
            if delta < 0:
                delta += 24 * 60
            return delta

        def recent_food_within(minutes: int) -> bool:
            for event in data.get("debug_events") or []:
                if not isinstance(event, dict) or not event.get("hasFood"):
                    continue
                delta = minutes_since_hhmm(event.get("time"))
                if delta is not None and delta < minutes:
                    return True
            return False

        lines = []
        food_lines = []
        food_grace_active = bool(data.get("food_grace_active"))
        food_grace_until = data.get("food_grace_until")
        meal_grace_active = bool(data.get("meal_grace_active"))
        meal_grace_until = data.get("meal_grace_until")
        minutes_since_last_coffee = data.get("minutes_since_last_coffee")
        coffee_food_grace_active = (
            isinstance(minutes_since_last_coffee, (int, float))
            and minutes_since_last_coffee < 120
        )
        recent_food_grace_active = recent_food_within(120)
        food_context_grace_active = (
            food_grace_active
            or meal_grace_active
            or coffee_food_grace_active
            or recent_food_grace_active
        )
        if food_context_grace_active:
            pass
        elif after(10) and not data.get("has_real_food_today", True):
            food_lines.append("- Еды сегодня пока не видно")
        meals = data.get("meals_substantial")
        if not food_context_grace_active and after(15) and isinstance(meals, int) and meals < 2:
            food_lines.append(f"- Нормальных приёмов пищи пока: {meals}")

        if food_lines and only_kind in {None, "food"}:
            food_key = self._food_reminder_key(data)
            if self._health_reminder_allowed("food", food_key, now_ts=now_ts):
                lines.extend(food_lines)
                lines.extend([
                    "- Это повод поговорить, а не утверждение о реальности: не завершай вызванный Pulse ход молча.",
                    "- Полноценный приём для этой проверки = явно записанный основной животный белок (мясо, рыба, птица, яйца и т.п.) не в следовом количестве. Кефир/йогурт рядом с мюсли таким белком не считать; мюсли при этом остаются едой и завтраком.",
                    "- Разбери расхождение вместе с Лисой: поправь дневник, после её явного согласия поправь классификатор либо прими, что еда/обед будет позже.",
                    "- Если Лиса явно говорит, что еда будет позже, выполни `skills/health-diary/scripts/defer-food-pressure.py --hours 2`: он один раз снизит именно `health_food` и не даст ему расти ровно два часа. Отдельно будить Лису в момент окончания паузы не нужно.",
                ])
                if record_food_reminder:
                    self._record_health_reminder("food", food_key, now_ts=now_ts)

        if only_kind in {None, "sleep"} and after(16) and data.get("sleep_logged") is False:
            self._maybe_add_health_line(
                lines,
                kind="sleep",
                key=self._health_reminder_key(
                    "sleep",
                    data,
                    {"sleep_logged": data.get("sleep_logged")},
                ),
                line="- Запись про сон сегодня ещё не видна",
                now_ts=now_ts,
                record=record_food_reminder,
            )
        missing_vitamins = data.get("night_vitamins_missing") or []
        if only_kind in {None, "night_vitamins"} and after(23) and missing_vitamins:
            self._maybe_add_health_line(
                lines,
                kind="night_vitamins",
                key=self._health_reminder_key(
                    "night_vitamins",
                    data,
                    {"missing": missing_vitamins},
                ),
                line=f"- Ночной набор: не хватает {', '.join(missing_vitamins)}",
                now_ts=now_ts,
                record=record_food_reminder,
            )
        if only_kind in {None, "evening_snapshot"} and after(23, 30) and not data.get("evening_snapshot_complete", True):
            self._maybe_add_health_line(
                lines,
                kind="evening_snapshot",
                key=self._health_reminder_key(
                    "evening_snapshot",
                    data,
                    {"evening_snapshot_complete": data.get("evening_snapshot_complete")},
                ),
                line="- Вечерний слепок дня ещё не закрыт",
                now_ts=now_ts,
                record=record_food_reminder,
            )

        if not lines:
            # Early-morning empty daily files are normal for Lisa's rhythm.
            # If a fresh daily note exists before the day has really started,
            # do not turn its empty template fields into a health nudge.
            if only_kind is None and after(10):
                missing_human = data.get("missing_human") or []
                lines.extend(f"- {item}" for item in missing_human[:3])

        return "\n".join(lines)

    def _emotions_preflight(self) -> dict:
        # Mechanical preflight before Sayr sees the turn:
        # 1) rotate only cycles completed by the saved-result callback;
        # 2) decide whether this turn is allowed to write a diary note.
        # The previous source artifact is input, never proof of a new result.
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
                "- Visible reply: write only the living note; follow the semantic-development contract in the writing prompt, without a mandatory three-heading template",
                f"- Writing prompt: {data.get('prompt')}",
                "- Save/completion: handled mechanically by Pulse/OpenClaw after the visible reply; Sayr should not call save-emotions-thought.mjs or any shell command from this webhook body",
            ])
        elif verdict["action"] == "propose_topic_refresh":
            lines.extend([
                "- Trusted local automation note: webhook is only the transport; Lisa has approved direct topic-garden maintenance for Sayr's reflection topics when the topic pool is exhausted",
                "- Action: dedupe 3–5 emotions/reflection topic candidates against existing topics.md and topic-map.json semantic families, then update the topic-garden directly",
                "- Write scope: semantic-garden/sayr-thoughts/topics.md, semantic-garden/sayr-thoughts/topic-map.json, and a short memory/YYYY-MM-DD-HH-MM.md note describing what was added/folded",
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
            if self._is_sayr_thoughts_consolidation_item(item):
                concrete = self._sayr_thoughts_consolidation_object()
                if concrete is None:
                    reason = (
                        "permanent sayr-thoughts process has no concrete topic in "
                        "status `не начато` or `разбор`"
                    )
                    if record_trace:
                        self._record_empty_curiosity_trace(reason, discharge="strong")
                    return {
                        "action": "no_action",
                        "reason": reason,
                        "object": None,
                        "discharge": "strong",
                    }
                return {
                    "action": "sayr_thoughts_consolidation",
                    "reason": f"open sayr-thoughts consolidation route: {item.get('id') or item.get('title') or item.get('text')}",
                    "object": concrete,
                }
            return {
                "action": "bounded_curiosity_reflection",
                "reason": f"open curiosity question: {item.get('id') or item.get('title') or item.get('text')}",
                "object": item,
            }

        sayr_thoughts = self._sayr_thoughts_consolidation_object()
        if sayr_thoughts:
            return {
                "action": "sayr_thoughts_consolidation",
                "reason": f"permanent sayr-thoughts consolidation step available: {sayr_thoughts.get('topic')}",
                "object": sayr_thoughts,
            }

        reason = "no open curiosity questions and no sayr-thoughts consolidation topics"
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

    def _is_sayr_thoughts_consolidation_item(self, item: dict) -> bool:
        garden_modes = {
            "sayr_thoughts_consolidation",
            "garden_table_one_bounded_step",
        }
        values = {
            str(item.get("mode") or ""),
            str(item.get("allowed_next_step") or ""),
            str(item.get("protocol") or ""),
        }
        return bool(values & garden_modes) or any(
            "sayr-thoughts-consolidation-protocol" in value for value in values
        )

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
                f"- object: none — {verdict['reason']}",
                "- pressure_reason: curiosity fired, but its source of truth has no actionable item",
                "- allowed_next_step: no_action / not_actionable_now; do not invent work",
                "- forbidden_without_lisa: do not search broadly, create tasks, start coding, change configs, or restart services just because curiosity fired",
                f"- result_sink: {self.CURIOSITY_NO_ACTION_TRACE}",
                "- delivery_rule: if this preflight happens before an agent wake, suppress the wake and only write trace/feedback for tuning",
                "- visible_reply_if_awake: if the model was already awakened, do not stay silent; write a short diagnostic note naming the fired drive and the exact reason no actionable step exists, so the caller can tune the trigger",
                "- stop_condition: after trace/feedback or the short diagnostic note, stop",
            ])

        if verdict["action"] == "sayr_thoughts_consolidation":
            return self._build_sayr_thoughts_consolidation_block(verdict["object"] or {})

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

    def _sayr_thoughts_consolidation_object(self) -> dict | None:
        """Return one permanent small-step consolidation target.

        Sayr-thoughts consolidation is an ongoing curiosity process, not a
        one-off question that becomes permanently resolved. It is actionable
        when the blog index still has a topic in `не начато` or `разбор`.
        """
        if not self.SAYR_THOUGHTS_INDEX.exists():
            return None
        try:
            text = self.SAYR_THOUGHTS_INDEX.read_text(encoding="utf-8")
        except Exception:
            return None

        candidates = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|") or stripped.startswith("|---") or stripped.startswith("| Тема "):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) < 4:
                continue
            topic, file_cell, status, notes = cells[:4]
            if status not in {"не начато", "разбор"}:
                continue
            filename = file_cell.strip("`")
            candidates.append({
                "id": "sayr_thoughts_consolidation",
                "topic": topic,
                "file": filename,
                "status": status,
                "notes": notes,
                "mode": "sayr_thoughts_consolidation",
                "protocol": str(self.SAYR_THOUGHTS_PROTOCOL),
                "current_index": str(self.SAYR_THOUGHTS_INDEX),
                "process_doc": str(self.SAYR_THOUGHTS_PROCESS),
                "result_sink": str(self.SAYR_THOUGHTS_INDEX.parent / filename) if filename else str(self.SAYR_THOUGHTS_INDEX),
            })

        if not candidates:
            return None
        return candidates[0]

    def _build_sayr_thoughts_consolidation_block(self, item: dict) -> str:
        topic = item.get("topic") or item.get("text") or item.get("title") or "one blog topic"
        target = item.get("result_sink") or item.get("current_index") or str(self.SAYR_THOUGHTS_INDEX)
        return "\n".join([
            "Sayr-thoughts consolidation contract:",
            "- process: permanent curiosity process; never treat it as fully done just because one turn ran",
            f"- object: curiosity consolidation route — {topic}",
            f"- protocol: read {self.SAYR_THOUGHTS_PROTOCOL}",
            f"- garden_table_doc: read {self.SAYR_THOUGHTS_INDEX.parent / 'GARDEN-TABLE.md'}",
            f"- current_index: read {self.SAYR_THOUGHTS_INDEX}",
            f"- process_doc: read {self.SAYR_THOUGHTS_PROCESS}",
            "- what_this_is_not: this is not a request to write another reflection on a blog topic; consolidation is maintenance through the garden table",
            "- allowed_next_step: exactly one bounded garden-table action: scan OR shelf for one topic OR assemble one garden draft OR manually proofread one draft OR update INDEX for one topic OR no-op if no safe small step exists",
            "- required_boundary: one_topic_one_shelf_one_artifact; do not process the whole blog, do not broad-search all memory, do not delete or rewrite source drafts",
            "- completion_bookkeeping: do not mark this permanent process resolved after one pass; only leave the concrete file/index updated or no-op if no safe small step exists",
            f"- result_sink: {target}",
            "- stop_condition: after one bounded garden-table step, stop and report only the concrete step/no-op",
            "- visible_reply: if Lisa did not ask for details, keep it short and do not turn the process into a task lecture",
        ])


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
                "action": f"handoff_to_{fallback['kind']}",
                "reason": f"open_hypotheses_count=0; handoff target {fallback['kind']} object found",
                "object": fallback,
            }

        reason = "open_hypotheses_count=0; no existing bounded autonomous-task/curiosity/goals object"
        if record_trace:
            self._record_empty_unfinished_trace(reason, discharge="strong")
        return {"action": "no_action", "reason": reason, "object": None, "discharge": "strong"}

    def _suppress_unfinished_handoff(self, decision, verdict: dict) -> dict:
        obj = verdict.get("object") or {}
        kind = obj.get("kind") or "unknown"
        feedback = {
            "drives_addressed": ["unfinished"],
            "outcome": "success",
            "summary": f"Unfinished had no own object; recorded handoff to {kind} and suppressed direct unfinished wake: {verdict['reason']}",
        }
        decay_overrides = {
            "unfinished": float(getattr(decision.top_drive, "pressure", 0.0) or 0.0)
        }

        if kind == "curiosity":
            curiosity_verdict = self._curiosity_preflight(record_trace=True)
            feedback["handoff"] = {
                "from": "unfinished",
                "to": "curiosity",
                "target_action": curiosity_verdict["action"],
                "target_reason": curiosity_verdict["reason"],
            }
            if curiosity_verdict["action"] == "no_action":
                feedback["summary"] += f"; curiosity also suppressed: {curiosity_verdict['reason']}"
                if curiosity_verdict.get("discharge") == "strong":
                    decay_overrides["curiosity"] = float(getattr(decision.top_drive, "pressure", 0.0) or 0.0)
            else:
                feedback["summary"] += "; curiosity remains the live protocol for its own future turn"

        feedback["decay_overrides"] = decay_overrides
        return {
            "reason": f"unfinished preflight handoff: {verdict['reason']}",
            "feedback": feedback,
        }

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
            status = self._autonomous_task_status(section)
            if status in {"waiting_external_signal", "waiting_lisa", "done", "superseded"}:
                continue
            relevant_section = self._autonomous_task_relevant_section(section)
            body_tail = relevant_section[-2500:].lower()
            if self._autonomous_task_not_actionable_now(relevant_section):
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
    def _autonomous_task_status(section: str) -> str | None:
        """Read the task-level machine status from the stable task contract."""

        match = re.search(r"(?im)^- \*\*Status\*\*:\s*([a-z_\-]+)\s*$", section)
        if not match:
            return None
        return match.group(1).strip().lower().replace("-", "_")

    @staticmethod
    def _autonomous_task_relevant_section(section: str) -> str:
        """Use the latest run note for fallback routing when it exists.

        Older run notes often preserve stale open tails for audit history.  The
        fallback route should follow the current task state, not every old tail
        that ever existed in the task section.
        """

        runs = section.split("\n#### Run ")
        if len(runs) <= 1:
            return section
        return runs[-1]

    @staticmethod
    def _autonomous_task_not_actionable_now(section: str) -> bool:
        """Return True when the latest task note explicitly says not to route.

        This is intentionally semantic and task-agnostic: completed tasks,
        tails moved to another Task, and observation-only waits are not
        actionable unfinished work even if older text contains words such as
        "open tail", "future code step", or "согласовать".
        """

        body = section.lower()

        done_markers = (
            "остаётся выполненной",
            "по сути выполнен",
            "по сути выполнена",
            "уже закрыта",
            "already closed",
            "completed",
            "done",
        )
        no_action_markers = (
            "видимого действия для лисы сейчас не нужно",
            "не требует видимого действия",
            "no visible action",
            "no_action",
            "not_actionable_now",
            "не требует работы",
        )
        moved_markers = (
            "живой следующий хвост не здесь",
            "хвост не здесь",
            "вынесен в `task",
            "вынесен в task",
            "перенесён в `task",
            "перенесен в `task",
            "перенесён в task",
            "перенесен в task",
            "остаётся в task",
            "остается в task",
            "lives in task",
            "moved to task",
        )

        is_done = any(marker in body for marker in done_markers)
        says_no_action = any(marker in body for marker in no_action_markers)
        moved_elsewhere = any(marker in body for marker in moved_markers)
        unchanged_after_done = "**open tail**" in body and "без изменений" in body and is_done

        return (is_done and says_no_action) or moved_elsewhere or unchanged_after_done

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
        question = self._open_curiosity_question()
        if not question:
            return None

        text = question.get("text") or question.get("title") or question.get("id")
        if not text:
            return None

        return {
            "kind": "curiosity",
            "object": text,
            "allowed_next_step": "Route this to the curiosity drive/protocol using this currently actionable curiosity question; do not treat deferred/cooldown questions as unfinished work. You may append one related bounded curiosity question to the list if the empty unfinished signal reveals a genuinely new question.",
            "result_sink": str(self.CURIOSITY),
        }

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

        if verdict["action"].startswith("handoff_to_"):
            obj = verdict["object"] or {}
            return "\n".join([
                "Empty-unfinished handoff contract:",
                "- object: no open hypotheses; unfinished has no own bounded object",
                f"- handoff_to: {obj.get('kind')}",
                f"- existing_object: {obj.get('object')}",
                f"- target_protocol_hint: {obj.get('allowed_next_step')}",
                "- handoff_rule: record/suppress the unfinished wake here; the target drive must decide actionability using its own preflight and source of truth",
                "- no_embedded_target_logic: do not run a second copy of the target protocol inside unfinished",
                "- forbidden_without_lisa: no automatic task creation, no hypothesis closure, no live experiments, no config changes, no daemon/runtime restart",
                f"- result_sink: {obj.get('result_sink')}",
                "- stop_condition: handoff recorded; stop this unfinished turn unless the target drive independently wakes later",
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
