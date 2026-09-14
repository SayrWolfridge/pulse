"""Exact-file stdlib acceptance; no Pulse global imports or external services."""
import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync = load("vault_sync_contract_owner", "src/core/vault_sync.py")
git = load("git_pulse_contract_owner", "src/integrations/sayr/git_pulse.py")
webhook = load("webhook_contract_owner", "src/core/webhook.py")


class VaultSyncContract(unittest.TestCase):
    def test_config_and_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            self.assertFalse(sync.load_config(path))
            config = dict(enabled=True, repo=sync.REPO, script=sync.SCRIPT,
                          origin=sync.ORIGIN, branch="master", interval_seconds=86400)
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertTrue(sync.load_config(path))
            config["interval_seconds"] = 300
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError):
                sync.load_config(path)
            config["interval_seconds"] = 86400
            config["origin"] = "https://public.example/repo"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError):
                sync.load_config(path)
        with patch.object(sync.subprocess, "run", return_value=SimpleNamespace(
                returncode=2, stdout='{"status":"blocked"}')) as run:
            self.assertEqual(sync.sync_once(), (2, "blocked"))
            code, status, payload = sync.sync_once(include_payload=True)
            self.assertEqual((code, status), (2, "blocked"))
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(run.call_args.args[0], ["python3", sync.SCRIPT,
                "--repo", sync.REPO, "--origin", sync.ORIGIN, "--branch", "master", "--phase", "daily", "--apply"])
            self.assertFalse(run.call_args.kwargs.get("shell", False))
            self.assertEqual(run.call_args.kwargs["timeout"], 70)
            sync.sync_once("finish")
            self.assertEqual(run.call_args.args[0][-3:], ["--phase", "finish", "--apply"])
            with self.assertRaises(ValueError):
                sync.sync_once("begin")

    def test_independent_worker_and_wiring(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "daily.json"
            worker = sync.VaultSyncWorker(state_path=state_path)
            def once():
                # Reservation is durable before invoking the transport.
                self.assertGreater(json.loads(state_path.read_text())["next_due"], sync.time.time())
                worker._stop.set()
                return (2, "blocked", {"status": "blocked", "reason": "fixture"})
            with patch.object(
                sync, "sync_once", side_effect=lambda phase, include_payload=False: once()
            ) as run:
                worker._run()
            run.assert_called_once_with("daily", include_payload=True)
            state = json.loads(state_path.read_text())
            self.assertEqual(state["last_result"], [2, "blocked"])
            self.assertTrue(86390 < state["next_due"] - sync.time.time() <= 86400)
            restarted = sync.VaultSyncWorker(state_path=state_path)
            with patch.object(sync, "sync_once") as run:
                with patch.object(restarted._stop, "wait", return_value=True) as wait:
                    restarted._run()
                run.assert_not_called()
                self.assertTrue(86390 < wait.call_args.args[0] <= 86400)
            state_path.write_text('{"next_due":"bad"}')
            with patch.object(sync, "sync_once") as run:
                sync.VaultSyncWorker(state_path=state_path)._run()
                run.assert_not_called()
        tree = ast.parse((ROOT / "src/core/daemon.py").read_text())
        calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
        self.assertIn("VaultSyncWorker", calls)
        self.assertIn("self.vault_sync.start", calls)
        source = ast.unparse(tree)
        self.assertIn("await asyncio.to_thread(self.vault_sync.stop)", source)
        self.assertIn("VaultSyncWorker(notify=self._submit_vault_sync_alert)", source)
        self.assertIn("asyncio.run_coroutine_threadsafe", source)

    def test_blocked_alert_is_durable_deduplicated_and_visibility_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "daily.json"
            sent = []
            worker = sync.VaultSyncWorker(
                state_path=state_path,
                notify=lambda message, alert_id: sent.append((message, alert_id)) or True,
            )
            worker._next_due = sync.time.time() + sync.INTERVAL
            payload = {
                "status": "blocked",
                "reason": "incoming paths overlap unfinished work; owner must reconcile",
                "before_head": "a" * 40,
                "behind": 1,
            }
            worker._handle_alert("daily", payload)
            self.assertEqual(len(sent), 1)
            persisted = json.loads(state_path.read_text())
            alert_id = persisted["alerts"][0]["id"]
            self.assertEqual(persisted["alerts"][0]["admission"], "accepted")
            self.assertFalse(persisted["alerts"][0]["completed"])
            self.assertIn("входящие коммиты и локальные незавершённые правки сохранены", sent[0][0])
            self.assertNotIn(payload["reason"], sent[0][0])

            # Admission is not visible delivery, but an unchanged conflict is
            # not replayed on every pass or process restart.
            worker._handle_alert("daily", payload)
            self.assertEqual(len(sent), 1)
            restarted = sync.VaultSyncWorker(
                state_path=state_path,
                notify=lambda message, alert_id: sent.append((message, alert_id)) or True,
            )
            with patch.object(restarted._stop, "wait", return_value=True):
                restarted._run()
            self.assertEqual(len(sent), 2)
            self.assertEqual(sent[0][1], sent[1][1])

            self.assertEqual(
                worker.mark_alert_terminal("0" * 64, "ok", "sent")["status"], "ignored"
            )
            terminal = worker.mark_alert_terminal(alert_id, "ok", "Короткое сообщение Лисе")
            self.assertEqual(terminal["status"], "terminal-output")
            worker._handle_alert("daily", payload)
            self.assertEqual(len(sent), 2)

            # A materially new state gets its own alert.
            changed = dict(payload, behind=2)
            worker._handle_alert("daily", changed)
            self.assertEqual(len(sent), 3)
            self.assertEqual(len(worker._alerts), 2)
            self.assertTrue(worker._alerts[alert_id]["completed"])
            new_id = next(alert for alert in worker._alerts if alert != alert_id)
            worker.mark_alert_terminal(new_id, "error", "")
            worker._handle_alert("daily", changed)
            self.assertEqual(len(sent), 4)

            worker._handle_alert("daily", {"status": "up_to_date", "uncommitted_count": 99})
            self.assertEqual(len(sent), 4)

        hook = webhook.OpenClawWebhook.__new__(webhook.OpenClawWebhook)
        kind = sync.ALERT_KIND_PREFIX + "a" * 64
        self.assertEqual(
            hook._result_callback_kind(f"x\nPULSE_VAULT_SYNC_CALLBACK_KIND={kind}"), kind
        )
        self.assertIsNone(hook._result_callback_kind("PULSE_VAULT_SYNC_CALLBACK_KIND=bad"))
        webhook_source = (ROOT / "src/core/webhook.py").read_text()
        self.assertIn("idempotency_key or", webhook_source)
        health_source = (ROOT / "src/core/health.py").read_text()
        self.assertIn("mark_alert_terminal", health_source)

    def test_real_git_lock_and_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            def command(*args):
                return subprocess.run(["git", *args], cwd=repo, check=True,
                    capture_output=True, text=True, timeout=5).stdout.strip()
            command("init", "-b", "master")
            command("config", "user.name", "Fixture")
            command("config", "user.email", "fixture@example.invalid")
            command("commit", "--allow-empty", "-m", "initial")
            before = command("rev-parse", "HEAD")
            (repo / "note.md").write_text("Fixture note\n")
            decision = SimpleNamespace(top_drive=SimpleNamespace(name="obsidian_git",
                source_data={"git": {"repo_path": str(repo), "repo_name": "obsidian"}}))
            lock = repo / ".git/vault-sync.lock"
            lock.write_text("foreign lock")
            blocked = git.execute_git_maintenance(decision, receipt_dir=Path(tmp) / "receipts")
            self.assertEqual(blocked.outcome, "blocked")
            self.assertEqual(command("rev-parse", "HEAD"), before)
            self.assertEqual(command("diff", "--cached", "--name-only"), "")
            self.assertEqual(lock.read_text(), "foreign lock")
            lock.unlink()
            def finish(phase, include_payload=False):
                self.assertEqual(phase, "finish")
                self.assertTrue(include_payload)
                self.assertFalse(lock.exists(), "finish must run AFTER lock release")
                self.assertNotEqual(command("rev-parse", "HEAD"), before)
                return (2, "blocked", {"status": "blocked", "reason": "fixture"})
            with patch.dict(sys.modules, {"pulse.src.core.vault_sync": sync}):
                with patch.object(sync, "REPO", str(repo)), patch.object(sync, "load_config", return_value=True):
                    with patch.object(sync, "sync_once", side_effect=finish) as exchange:
                        result = git.execute_git_maintenance(decision, receipt_dir=Path(tmp) / "receipts")
                        exchange.assert_called_once_with("finish", include_payload=True)
            self.assertEqual(result.outcome, "committed")
            self.assertEqual(result.exchange_status, "blocked")
            self.assertTrue(result.needs_attention)
            self.assertFalse(result.resolves_drive)
            self.assertIn("needs_attention: true", result.as_message())
            self.assertEqual(json.loads(Path(result.receipt_path).read_text())["exchange_status"], "blocked")
            self.assertFalse(lock.exists())
            self.assertNotEqual(command("rev-parse", "HEAD"), before)

            # A second actual commit with an unrelated disallowed file still
            # reaches finish, but does not claim all Git work resolved.
            (repo / "second.md").write_text("Second fixture note\n")
            (repo / "manual.bin").write_bytes(b"preserve me")
            with patch.dict(sys.modules, {"pulse.src.core.vault_sync": sync}):
                with patch.object(sync, "REPO", str(repo)), patch.object(sync, "load_config", return_value=True):
                    with patch.object(
                        sync, "sync_once", return_value=(0, "synced", {"status": "synced"})
                    ) as exchange:
                        result = git.execute_git_maintenance(decision, receipt_dir=Path(tmp) / "receipts")
                        exchange.assert_called_once_with("finish", include_payload=True)
                        self.assertEqual(result.outcome, "committed_partial")
                        self.assertTrue(result.needs_attention)
                        self.assertFalse(result.resolves_drive)
                        self.assertEqual((repo / "manual.bin").read_bytes(), b"preserve me")
                        git.execute_git_maintenance(decision, receipt_dir=Path(tmp) / "receipts")
                        exchange.assert_called_once()  # no commit, no finish replay

    def test_finish_is_opt_in_and_obsidian_only(self):
        with patch.object(sync, "load_config", return_value=True) as config:
            with patch.object(sync, "sync_once") as run:
                self.assertIsNone(sync.finish_after_commit("/tmp/not-obsidian"))
                config.assert_not_called()
                run.assert_not_called()
        with patch.object(sync, "load_config", return_value=False):
            with patch.object(sync, "sync_once") as run:
                self.assertIsNone(sync.finish_after_commit(sync.REPO))
                run.assert_not_called()
        with patch.object(sync, "load_config", side_effect=ValueError("bad config")):
            self.assertEqual(sync.finish_after_commit(sync.REPO), (1, "ValueError"))

        alerts = []
        owner = SimpleNamespace(_handle_alert=lambda phase, payload: alerts.append((phase, payload)))
        with patch.object(sync, "_active_worker", owner):
            with patch.object(sync, "load_config", return_value=True):
                with patch.object(sync, "sync_once", return_value=(2, "blocked", {"status": "blocked"})):
                    self.assertEqual(sync.finish_after_commit(sync.REPO), (2, "blocked"))
        self.assertEqual(alerts, [])

        alerts.clear()
        with patch.object(sync, "_active_worker", owner):
            with patch.object(sync, "load_config", side_effect=OSError("fixture")):
                self.assertEqual(sync.finish_after_commit(sync.REPO), (1, "OSError"))
        self.assertEqual(alerts, [])

    def test_finish_has_one_notification_owner_and_daily_alert_uses_brain_contract(self):
        alert = sync._alert_for_result(
            "daily",
            {"status": "blocked", "reason": "remote has new commits"},
        )
        self.assertIsNotNone(alert)
        self.assertIn("сессии «Настройки»", alert["message"])
        self.assertIn("безопасную read-only диагностику", alert["message"])
        self.assertIn("Не ограничивайся фразой «не получилось»", alert["message"])


if __name__ == "__main__":
    unittest.main()
