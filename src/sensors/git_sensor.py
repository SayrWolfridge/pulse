"""
Git Sensor — Phase 3 Integration

Monitors configured git repositories for local hygiene signals:
uncommitted changes, untracked files, unpushed commits, and whether
the local branch is behind remote.

These signals feed the agent's ``goals`` and ``growth`` drives —
nudging it to commit, push, or pull when things drift.

Architecture:
  - Runs ``git`` subprocesses asynchronously (no external deps).
  - Falls back gracefully when git is unavailable or path is not a repo.
  - Tracks per-repo state across reads for stale-push detection.
  - Reports: uncommitted_changes, untracked_files, commits_ahead,
    commits_behind, stale_push, last_commit_minutes_ago

Config (pulse.yaml):
  sensors:
    git:
      enabled: true
      repos:
        - ~/workspace/pulse           # paths to watch (~ expanded)
        - ~/workspace/my-project
      stale_push_minutes: 60          # threshold before "stale_push" fires
      fetch_remote: false             # set true to run `git fetch` each cycle
                                      # (makes behind-check accurate; costs network)
      request_timeout: 10             # per-subprocess timeout (seconds)
"""

import ast
import asyncio
import fnmatch
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from pulse.src.core.config import PulseConfig
from pulse.src.sensors.manager import BaseSensor

logger = logging.getLogger("pulse.sensors.git")


@dataclass
class _RepoState:
    """Tracks per-repo timing for stale-push detection."""
    first_seen_ahead_ts: Optional[float] = None   # epoch when commits_ahead first noticed
    last_commit_ts: Optional[float] = None         # epoch of most recent commit
    last_pressure_fingerprint: Optional[str] = None
    waiting_for_user: bool = False
    waiting_reason: Optional[str] = None


class GitSensor(BaseSensor):
    """Monitor git repositories for work-hygiene signals.

    Feeds:
      - ``git.uncommitted_changes`` → ``goals`` drive spike when dirty
      - ``git.stale_push``          → ``goals`` drive spike when unpushed too long
      - ``git.commits_behind``      → ``growth`` drive spike when updates available
    """

    name = "git"

    def __init__(self, config: PulseConfig):
        self.config = config
        self.git_cfg = config.sensors.git
        self._repo_states: Dict[str, _RepoState] = {}
        self._repo_meta: Dict[str, Dict[str, Any]] = {}

    _ARTIFACT_SUFFIXES = (
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".zip", ".out", ".json"
    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Validate repo paths and confirm git is available."""
        git_ok = await self._git_available()
        if not git_ok:
            logger.warning("Git sensor: `git` binary not found — sensor disabled")
            return

        valid = []
        for raw in self.git_cfg.repos:
            repo_cfg = raw if isinstance(raw, dict) else {"path": raw}
            repo_path = repo_cfg.get("path")
            if not isinstance(repo_path, str) or not repo_path.strip():
                logger.warning("Git sensor: invalid repo config (missing path): %s", raw)
                continue
            p = Path(repo_path).expanduser().resolve()
            if p.is_dir():
                path_str = str(p)
                valid.append(path_str)
                self._repo_states[path_str] = _RepoState()
                self._repo_meta[path_str] = repo_cfg
            else:
                logger.warning("Git sensor: repo path does not exist: %s", raw)

        if valid:
            logger.info(
                "Git sensor initialised — watching %d repo(s): %s",
                len(valid),
                ", ".join(valid),
            )
        else:
            logger.warning("Git sensor: no valid repo paths configured")

    async def stop(self) -> None:
        pass  # no persistent resources

    # ------------------------------------------------------------------
    # Main read
    # ------------------------------------------------------------------

    async def read(self) -> dict:
        """Poll all configured repos; return aggregated status."""
        if not self._repo_states:
            return self._empty_result()

        repo_results: List[dict] = []
        for repo_path in self._repo_states:
            result = await self._check_repo(repo_path)
            repo_results.append(result)

        # Aggregate across repos (any positive → surface it)
        total_uncommitted = sum(r["uncommitted_changes"] for r in repo_results)
        total_untracked   = sum(r["untracked_files"] for r in repo_results)
        total_pressure_uncommitted = sum(r.get("pressure_uncommitted_changes", r["uncommitted_changes"]) for r in repo_results)
        total_pressure_untracked = sum(r.get("pressure_untracked_files", r["untracked_files"]) for r in repo_results)
        total_ignored_pressure = sum(r.get("ignored_pressure_files", 0) for r in repo_results)
        total_ahead       = sum(r["commits_ahead"] for r in repo_results)
        total_behind      = sum(r["commits_behind"] for r in repo_results)
        any_stale_push    = any(r["stale_push"] for r in repo_results)

        # Soonest last-commit (most recent activity across all repos)
        commit_ages = [r["last_commit_minutes_ago"] for r in repo_results
                       if r["last_commit_minutes_ago"] is not None]
        last_commit_minutes_ago: Optional[float] = min(commit_ages) if commit_ages else None

        result = {
            "repos": repo_results,
            "uncommitted_changes": total_uncommitted > 0,
            "untracked_files": total_untracked,
            "pressure_uncommitted_changes": total_pressure_uncommitted,
            "pressure_untracked_files": total_pressure_untracked,
            "ignored_pressure_files": total_ignored_pressure,
            "pressure_dirty": (total_pressure_uncommitted + total_pressure_untracked) > 0,
            "commits_ahead": total_ahead,
            "commits_behind": total_behind,
            "stale_push": any_stale_push,
            "last_commit_minutes_ago": last_commit_minutes_ago,
            "timestamp": time.time(),
        }

        logger.debug(
            "Git sensor: dirty=%s untracked=%d ahead=%d behind=%d stale=%s",
            result["uncommitted_changes"],
            result["untracked_files"],
            result["commits_ahead"],
            result["commits_behind"],
            result["stale_push"],
        )
        return result

    # ------------------------------------------------------------------
    # Per-repo checks
    # ------------------------------------------------------------------

    async def _check_repo(self, repo_path: str) -> dict:
        """Run git commands against a single repo and return its status dict."""
        state = self._repo_states[repo_path]
        now = time.time()
        stale_threshold_sec = self.git_cfg.stale_push_minutes * 60

        # --- Is this a git repo at all? ---
        is_repo = await self._run_git(["rev-parse", "--git-dir"], repo_path)
        if is_repo is None:
            logger.debug("Git sensor: %s is not a git repo (or git unavailable)", repo_path)
            return self._empty_repo_result(repo_path)

        # --- Uncommitted changes (staged + unstaged) ---
        # Use -z so paths with non-ASCII/spaces are not C-quoted; tests and older
        # mocks may still return newline-delimited porcelain, so the parser handles both.
        status_out = await self._run_git(["status", "--porcelain=v1", "-z"], repo_path)
        repo_meta = self._repo_meta.get(repo_path, {})
        status_entries = self._parse_status_output(status_out or "")
        ignore_pressure_patterns = self._ignore_pressure_patterns(repo_meta)
        uncommitted_changes = 0
        untracked_files = 0
        pressure_uncommitted_changes = 0
        pressure_untracked_files = 0
        ignored_pressure_files = 0
        for entry in status_entries:
            if entry["untracked"]:
                untracked_files += 1
            else:
                uncommitted_changes += 1

            if self._matches_any(entry["path"], ignore_pressure_patterns):
                ignored_pressure_files += 1
                continue
            if entry["untracked"]:
                pressure_untracked_files += 1
            else:
                pressure_uncommitted_changes += 1

        artifact_only_tail = self._is_artifact_only_tail(status_entries)

        # --- Commits ahead/behind remote ---
        commits_ahead = 0
        commits_behind = 0

        # Optionally fetch to get accurate remote state
        if self.git_cfg.fetch_remote:
            await self._run_git(["fetch", "--quiet"], repo_path)

        rev_list_out = await self._run_git(
            ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            repo_path,
        )
        if rev_list_out is not None:
            parts = rev_list_out.strip().split()
            if len(parts) == 2:
                try:
                    commits_ahead = int(parts[0])
                    commits_behind = int(parts[1])
                except ValueError:
                    pass

        # --- Stale push detection ---
        if commits_ahead > 0:
            if state.first_seen_ahead_ts is None:
                state.first_seen_ahead_ts = now
            time_ahead_sec = now - state.first_seen_ahead_ts
            stale_push = time_ahead_sec >= stale_threshold_sec
        else:
            state.first_seen_ahead_ts = None
            stale_push = False

        pressure_fingerprint = self._pressure_fingerprint(
            pressure_uncommitted_changes=pressure_uncommitted_changes,
            pressure_untracked_files=pressure_untracked_files,
            ignored_pressure_files=ignored_pressure_files,
            commits_ahead=commits_ahead,
            stale_push=stale_push,
            artifact_only_tail=artifact_only_tail,
        )
        unchanged_pressure_tail = (
            state.last_pressure_fingerprint == pressure_fingerprint
            and pressure_fingerprint is not None
        )
        state.last_pressure_fingerprint = pressure_fingerprint

        waiting_for_user = bool(repo_meta.get("waiting_for_user"))
        waiting_reason = repo_meta.get("waiting_reason") if waiting_for_user else None
        state.waiting_for_user = waiting_for_user
        state.waiting_reason = waiting_reason

        # --- Last commit timestamp ---
        last_commit_ts_str = await self._run_git(
            ["log", "-1", "--format=%ct"], repo_path
        )
        last_commit_minutes_ago: Optional[float] = None
        if last_commit_ts_str and last_commit_ts_str.strip().isdigit():
            commit_epoch = float(last_commit_ts_str.strip())
            state.last_commit_ts = commit_epoch
            last_commit_minutes_ago = round((now - commit_epoch) / 60, 1)

        return {
            "path": repo_path,
            "name": repo_meta.get("name"),
            "drives": repo_meta.get("drives", []),
            "uncommitted_changes": uncommitted_changes,
            "untracked_files": untracked_files,
            "pressure_uncommitted_changes": pressure_uncommitted_changes,
            "pressure_untracked_files": pressure_untracked_files,
            "ignored_pressure_files": ignored_pressure_files,
            "pressure_dirty": (pressure_uncommitted_changes + pressure_untracked_files) > 0,
            "artifact_only_tail": artifact_only_tail,
            "unchanged_pressure_tail": unchanged_pressure_tail,
            "waiting_for_user": waiting_for_user,
            "waiting_reason": waiting_reason,
            "commits_ahead": commits_ahead,
            "commits_behind": commits_behind,
            "stale_push": stale_push,
            "last_commit_minutes_ago": last_commit_minutes_ago,
        }

    @staticmethod
    def _parse_status_output(status_out: str) -> List[dict]:
        """Parse git porcelain v1 status into {path, untracked} entries."""
        if not status_out:
            return []
        raw_entries = status_out.split("\0") if "\0" in status_out else status_out.splitlines()
        entries: List[dict] = []
        skip_next = False
        for raw in raw_entries:
            if skip_next:
                skip_next = False
                continue
            if not raw:
                continue
            if len(raw) < 3:
                continue
            status = raw[:2]
            path = raw[3:]
            # Porcelain -z encodes renames as one entry followed by the original path.
            if "\0" in status_out and status[0] in {"R", "C"}:
                skip_next = True
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if path.startswith('"') and path.endswith('"'):
                try:
                    path = ast.literal_eval(path)
                except Exception:
                    path = path.strip('"')
            entries.append({"path": path, "untracked": status == "??"})
        return entries

    @staticmethod
    def _ignore_pressure_patterns(repo_meta: Dict[str, Any]) -> List[str]:
        patterns = list(repo_meta.get("ignore_pressure_patterns") or [])
        # Sayr thoughts are generated often and are handled by slower memory hygiene;
        # by themselves they should not wake workspace_git every Pulse tick.
        if repo_meta.get("name") == "workspace":
            patterns.append("memory/sayr-thoughts/**")
        return patterns

    @classmethod
    def _is_artifact_only_tail(cls, entries: List[dict]) -> bool:
        if not entries:
            return False
        for entry in entries:
            path = entry.get("path", "")
            name = Path(path).name
            if path.startswith("reports/") or name.startswith("tmp-"):
                continue
            if name.endswith(cls._ARTIFACT_SUFFIXES):
                continue
            return False
        return True

    @staticmethod
    def _pressure_fingerprint(
        *,
        pressure_uncommitted_changes: int,
        pressure_untracked_files: int,
        ignored_pressure_files: int,
        commits_ahead: int,
        stale_push: bool,
        artifact_only_tail: bool,
    ) -> Optional[str]:
        if not any([
            pressure_uncommitted_changes,
            pressure_untracked_files,
            ignored_pressure_files,
            commits_ahead,
            stale_push,
            artifact_only_tail,
        ]):
            return None
        return (
            f"c{pressure_uncommitted_changes}:u{pressure_untracked_files}:"
            f"i{ignored_pressure_files}:a{commits_ahead}:s{int(stale_push)}:"
            f"artifact{int(artifact_only_tail)}"
        )

    @staticmethod
    def _matches_any(path: str, patterns: List[str]) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)

    # ------------------------------------------------------------------
    # git subprocess helper
    # ------------------------------------------------------------------

    async def _run_git(
        self, args: List[str], cwd: str
    ) -> Optional[str]:
        """Run `git <args>` in `cwd`, return stdout string or None on error."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self.git_cfg.request_timeout
            )
            if proc.returncode == 0:
                return stdout.decode(errors="replace")
            return None
        except (asyncio.TimeoutError, OSError, FileNotFoundError):
            return None

    async def _git_available(self) -> bool:
        """Return True if `git --version` succeeds."""
        result = await self._run_git(["--version"], cwd="/tmp")
        return result is not None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_result() -> dict:
        return {
            "repos": [],
            "uncommitted_changes": False,
            "untracked_files": 0,
            "pressure_uncommitted_changes": 0,
            "pressure_untracked_files": 0,
            "ignored_pressure_files": 0,
            "pressure_dirty": False,
            "commits_ahead": 0,
            "commits_behind": 0,
            "stale_push": False,
            "last_commit_minutes_ago": None,
            "timestamp": time.time(),
        }

    @staticmethod
    def _empty_repo_result(path: str) -> dict:
        return {
            "path": path,
            "uncommitted_changes": 0,
            "untracked_files": 0,
            "pressure_uncommitted_changes": 0,
            "pressure_untracked_files": 0,
            "ignored_pressure_files": 0,
            "pressure_dirty": False,
            "commits_ahead": 0,
            "commits_behind": 0,
            "stale_push": False,
            "last_commit_minutes_ago": None,
        }
