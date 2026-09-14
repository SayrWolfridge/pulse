"""Opt-in private-vault exchange, independent of cognitive/model scheduling."""

import hashlib
import json
import logging
import math
import subprocess
import threading
import time
from concurrent.futures import Future
from pathlib import Path

logger = logging.getLogger("pulse")
CONFIG_PATH = Path("~/.pulse/config/vault-sync.json").expanduser()
STATE_PATH = Path("~/.pulse/state/vault-sync-worker.json").expanduser()
REPO = str(Path.home() / "Obsidian")
SCRIPT = REPO + "/lair/infra/scripts/vault_git_sync.py"


def _configured_origin(path=CONFIG_PATH):
    """Keep the private transport endpoint in machine-local config, not source."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "vault-host:D:/Git/obsidian.git"
    origin = data.get("origin") if isinstance(data, dict) else None
    return origin if isinstance(origin, str) and origin else "vault-host:D:/Git/obsidian.git"


ORIGIN = _configured_origin()
INTERVAL = 86400
TIMEOUT = 70
ALERT_KIND_PREFIX = "pulse.vault_sync.alert:"
_active_worker = None


def alert_id_from_callback_kind(kind):
    if not isinstance(kind, str) or not kind.startswith(ALERT_KIND_PREFIX):
        return None
    alert_id = kind[len(ALERT_KIND_PREFIX):]
    if len(alert_id) != 64 or any(c not in "0123456789abcdef" for c in alert_id):
        return None
    return alert_id


def _alert_for_result(phase, payload):
    """Build a bounded, content-free Settings work item for a stopped exchange."""
    status = str(payload.get("status", "unknown"))
    if status not in {"blocked", "error"}:
        return None
    reason = str(payload.get("reason", ""))
    if "incoming paths overlap unfinished work" in reason:
        action = "получить входящие коммиты"
        preservation = "входящие коммиты и локальные незавершённые правки сохранены; рабочие файлы не менялись"
        decision = "разбираем пересечение сейчас или оставляем его владельцам до следующего прохода"
    elif "committed paths overlap across histories" in reason:
        action = "объединить и обменять разошедшиеся истории"
        preservation = "обе истории сохранены; merge, reset и выбор победителя не выполнялись"
        decision = "какую смысловую редакцию принять при ручном объединении"
    elif "remote has new commits" in reason:
        action = "отправить локальные коммиты"
        preservation = "локальная и удалённая истории сохранены; история не переписывалась"
        decision = "сначала разбираем входящие изменения или откладываем отправку"
    elif "staged work owns the index" in reason:
        action = "получить входящие коммиты"
        preservation = "входящие коммиты и локальный индекс сохранены; staged-правки не менялись"
        decision = "кто завершает текущий staged-пакет перед повторным обменом"
    elif "archived-media paths" in reason:
        action = "получить входящие архивные медиа"
        preservation = "обе Git-истории сохранены; медиа автоматически не переносились"
        decision = "разбираем архивный пакет сейчас или оставляем до ручной проверки"
    elif status == "error":
        action = "завершить Git-обмен"
        preservation = "локальные рабочие файлы не менялись; состояние второй стороны не подтверждено"
        decision = "проверяем сбой сейчас или ждём следующего штатного прохода"
    else:
        action = "завершить Git-обмен"
        preservation = "автоматического merge, reset, stash или выбора победителя не было"
        decision = "разбираем остановку сейчас или оставляем её владельцам"
    identity = {
        "phase": phase,
        "status": status,
        "reason": reason,
        "before_head": payload.get("before_head"),
        "ahead": payload.get("ahead"),
        "behind": payload.get("behind"),
    }
    alert_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    message = "\n".join([
        "[PULSE GIT-SYNC ALERT]",
        "Это рабочий вход для постоянной сессии «Настройки», а не текст для механического пересказа.",
        f"Не удалось {action}.",
        f"По фактам: {preservation}.",
        "Сначала свяжи результат с текущим контекстом этой сессии и проведи безопасную read-only диагностику, если она нужна.",
        "Если Лиса уже разрешила подходящее продолжение в этой сессии — закончи его штатным способом и сообщи итог.",
        f"Если действительно нужен выбор Лисы, задай один точный вопрос: {decision}?",
        "Не ограничивайся фразой «не получилось». Не показывай тексты заметок, секреты, пути файлов или сырой diff и не придумывай разрешение на Git-мутации.",
        f"PULSE_VAULT_SYNC_CALLBACK_KIND={ALERT_KIND_PREFIX}{alert_id}",
    ])
    return {"id": alert_id, "message": message, "completed": False,
            "admission": "pending", "status": status, "phase": phase}


def load_config(path=CONFIG_PATH):
    """Machine-local enablement only; never accept arbitrary commands/targets."""
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("vault-sync config must be an object")
    if data.get("enabled", False) is False:
        return False
    expected = {"enabled": True, "repo": REPO, "script": SCRIPT,
                "origin": ORIGIN, "branch": "master", "interval_seconds": INTERVAL}
    if data.get("enabled") is not True or data != expected:
        raise ValueError("vault-sync config does not match the private-vault contract")
    return True


def sync_once(phase="daily", include_payload=False):
    if phase not in ("daily", "finish"):
        raise ValueError("unsupported Pulse vault-sync phase")
    result = subprocess.run(
        ["python3", SCRIPT, "--repo", REPO, "--origin", ORIGIN,
         "--branch", "master", "--phase", phase, "--apply"],
        cwd=REPO, capture_output=True, text=True, timeout=TIMEOUT, check=False,
    )
    if result.returncode not in (0, 1, 2):
        raise RuntimeError("vault-sync helper returned unexpected exit code")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("vault-sync helper did not return a JSON object")
    # Do not log file paths, contents, stderr or transport credentials.
    summary = (result.returncode, str(payload.get("status", payload.get("outcome", "unknown"))))
    return (*summary, payload) if include_payload else summary


def finish_after_commit(repo_path):
    """Run exchange after autocommit; the caller owns the single final notice."""
    if Path(repo_path).resolve() != Path(REPO).resolve():
        return None
    try:
        if not load_config():
            return None
        code, status, payload = sync_once("finish", include_payload=True)
        return code, status
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return (1, type(exc).__name__)


class VaultSyncWorker:
    def __init__(self, config_path=CONFIG_PATH, state_path=STATE_PATH, notify=None):
        self.config_path = config_path
        self.state_path = state_path
        self._stop = threading.Event()
        self._thread = None
        self._last_result = None
        self._next_due = 0
        self._alerts = {}
        self._notify = notify
        self._state_lock = threading.Lock()

    def start(self):
        global _active_worker
        try:
            enabled = load_config(self.config_path)
        except (OSError, ValueError):
            logger.warning("VAULT SYNC: invalid machine-local config; disabled")
            return
        if not enabled or self._thread is not None:
            return
        _active_worker = self
        self._thread = threading.Thread(target=self._run, name="pulse-vault-sync", daemon=True)
        self._thread.start()

    def _run(self):
        # Reserve the next due time before I/O: a process restart must not turn
        # a blocked exchange into frequent retries. Corrupt state fails closed.
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
            next_due = state.get("next_due", 0)
            if (type(next_due) not in (int, float) or not math.isfinite(next_due)
                    or next_due < 0 or next_due > time.time() + INTERVAL):
                raise ValueError("invalid daily due time")
            last = state.get("last_result")
            self._last_result = tuple(last) if isinstance(last, list) and len(last) == 2 else None
            alerts = state.get("alerts", [])
            if not isinstance(alerts, list) or any(not isinstance(item, dict) for item in alerts):
                raise ValueError("invalid alert queue")
            self._alerts = {
                item["id"]: item for item in alerts
                if isinstance(item.get("id"), str)
            }
            self._next_due = next_due
        except (OSError, ValueError, AttributeError):
            logger.warning("VAULT SYNC: invalid daily state; disabled")
            return
        self._retry_unfinished_alerts()
        while not self._stop.is_set():
            delay = max(0, next_due - time.time())
            if delay and self._stop.wait(delay):
                return
            next_due = time.time() + INTERVAL
            self._next_due = next_due
            try:
                self._save_state(next_due)
            except OSError:
                logger.warning("VAULT SYNC: cannot persist daily due time; disabled")
                return
            try:
                code, status, payload = sync_once("daily", include_payload=True)
                result = (code, status)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                result = (1, type(exc).__name__)
                payload = {"status": "error", "reason": type(exc).__name__}
            if result != self._last_result:
                logger.info("VAULT SYNC: exit=%s status=%s", *result)
                self._last_result = result
            self._handle_alert("daily", payload)
            try:
                self._save_state(next_due)
            except OSError:
                logger.warning("VAULT SYNC: cannot persist daily result; disabled")
                return

    def _save_state(self, next_due=None):
        with self._state_lock:
            if next_due is not None:
                self._next_due = next_due
            unfinished = [item for item in self._alerts.values() if not item.get("completed")]
            completed = sorted(
                (item for item in self._alerts.values() if item.get("completed")),
                key=lambda item: item.get("terminal_at", 0),
            )[-32:]
            self._alerts = {item["id"]: item for item in unfinished + completed}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            candidate = self.state_path.with_suffix(".tmp")
            candidate.write_text(json.dumps({"next_due": self._next_due,
                "last_result": self._last_result,
                "alerts": list(self._alerts.values())}), encoding="utf-8")
            candidate.replace(self.state_path)

    def _handle_alert(self, phase, payload):
        candidate = _alert_for_result(phase, payload)
        if candidate is None:
            return
        existing = self._alerts.get(candidate["id"])
        if existing:
            if existing.get("completed"):
                return
            if (existing.get("admission") in {"pending", "accepted"}
                    and existing.get("delivery") != "not-visible"):
                return
            candidate = dict(existing)
        else:
            self._alerts[candidate["id"]] = candidate
            self._save_state()
        if self._notify is None:
            return
        candidate["last_attempt_at"] = time.time()
        candidate["admission"] = "pending"
        self._alerts[candidate["id"]] = candidate
        self._save_state()
        try:
            admission = self._notify(candidate["message"], candidate["id"])
        except Exception:
            self._record_admission(candidate["id"], "error")
            return
        if isinstance(admission, Future):
            admission.add_done_callback(
                lambda future, alert_id=candidate["id"]: self._admission_done(alert_id, future)
            )
        else:
            self._record_admission(candidate["id"], admission)

    def _admission_done(self, alert_id, future):
        try:
            self._record_admission(alert_id, future.result())
        except Exception:
            self._record_admission(alert_id, "error")

    def _record_admission(self, alert_id, result):
        alert = self._alerts.get(alert_id)
        if alert is None:
            return
        alert["admission"] = (
            "accepted" if result is True else "rejected" if result is False
            else "ambiguous" if result is None else "error"
        )
        self._save_state()

    def mark_alert_terminal(self, alert_id, status, output_text):
        alert = self._alerts.get(alert_id)
        if alert is None:
            return {"status": "ignored", "reason": "stale-or-unknown-alert"}
        terminal_output = status == "ok" and isinstance(output_text, str) and bool(output_text.strip())
        alert["terminal_status"] = str(status or "unknown")
        alert["terminal_output"] = terminal_output
        alert["completed"] = terminal_output
        # A terminal output is stronger than hook admission, but Telegram-visible
        # delivery remains a live canary and is never inferred from this callback.
        alert["delivery"] = "terminal-output" if terminal_output else "not-visible"
        alert["terminal_at"] = time.time()
        self._save_state()
        return {"status": alert["delivery"], "alert_id": alert_id}

    def _retry_unfinished_alerts(self):
        if self._notify is None:
            return
        for alert in list(self._alerts.values()):
            if alert.get("completed"):
                continue
            # Replays use the same durable idempotency key, so a restart cannot
            # create a second hook run for the same unchanged conflict.
            alert["admission"] = "retry"
            self._alerts[alert["id"]] = alert
            self._save_state()
            try:
                admission = self._notify(alert["message"], alert["id"])
            except Exception:
                self._record_admission(alert["id"], "error")
                continue
            if isinstance(admission, Future):
                admission.add_done_callback(
                    lambda future, alert_id=alert["id"]: self._admission_done(alert_id, future)
                )
            else:
                self._record_admission(alert["id"], admission)

    def stop(self):
        global _active_worker
        self._stop.set()
        if self._thread is not None:
            # Let the bounded helper release its lock; do not orphan a sync.
            self._thread.join(TIMEOUT + 5)
        if _active_worker is self:
            _active_worker = None
