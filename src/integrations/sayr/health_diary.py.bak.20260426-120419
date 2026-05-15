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
    EMOTIONAL_LANDSCAPE = Path("/home/lisa/.openclaw/workspace/pulse/self/emotional-landscape.json")
    GOALS_SNAPSHOT = Path("/home/lisa/.openclaw/workspace/pulse/self/goals-snapshot.json")

    def suppress_trigger(self, decision, config) -> dict | None:
        if not decision.top_drive:
            return None

        if decision.top_drive.name == "health":
            block = self._build_health_block()
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
            if verdict["action"] == "write_diary_note":
                return None
            return {
                "reason": f"emotions preflight: {verdict['reason']}",
                "feedback": {
                    "drives_addressed": ["emotions"],
                    "outcome": "success",
                    "summary": f"Emotions preflight suppressed agent wake: {verdict['reason']}",
                },
            }

        return None

    def build_trigger_message(self, decision, config) -> str:
        base = super().build_trigger_message(decision, config)
        if not decision.top_drive:
            return base

        if decision.top_drive.name == "health":
            block = self._build_health_block()
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

        return base

    def _build_health_block(self) -> str:
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
        if after(10) and not data.get("has_real_food_today", True):
            lines.append("- Еды сегодня пока не видно")
        meals = data.get("meals_substantial")
        if after(15) and isinstance(meals, int) and meals < 2:
            lines.append(f"- Нормальных приёмов пищи пока: {meals}")
        if after(14) and data.get("sleep_logged") is False:
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
        elif last_age_hours is not None and last_age_hours < cooldown_hours:
            action = "do_not_write"
            reason = f"cooldown-active ({last_age_hours:.1f}h < {cooldown_hours:.0f}h)"
        elif not prompt:
            action = "do_not_write"
            reason = "no-clean-prompt"

        return {"action": action, "reason": reason, "data": data}

    def _build_emotions_block(self) -> str:
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

        notes = data.get("notes")
        if notes:
            lines.append(f"- Source signal: {notes}")

        if verdict["action"] == "write_diary_note":
            lines.extend([
                "- Visible reply: write only the diary note in three beats: `Что крутится` / `Что я про это понимаю` / `Что хочется дальше`",
                f"- Writing prompt: {data.get('prompt')}",
                "- After composing the exact visible text, call `node /home/lisa/.openclaw/workspace/scripts/save-emotions-thought.mjs --text \"<exact same text>\"`; do not send a separate 'saved' status if it succeeds",
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
