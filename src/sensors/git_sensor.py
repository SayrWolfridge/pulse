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

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from pulse.src.core.config import PulseConfig
from pulse.src.sensors.manager import BaseSensor

logger = logging.getLogger("pulse.sensors.git")


@dataclass
class _RepoState:
    """Tracks per-repo timing for stale-push detection."""
    first_seen_ahead_ts: Optional[float] = None   # epoch when commits_ahead first noticed
    last_commit_ts: Optional[float] = None         # epoch of most recent commit


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
            p = Path(raw).expanduser().resolve()
            if p.is_dir():
                valid.append(str(p))
                self._repo_states[str(p)] = _RepoState()
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
        status_out = await self._run_git(["status", "--porcelain"], repo_path)
        uncommitted_changes = 0
        untracked_files = 0
        if status_out is not None:
            for line in status_out.strip().splitlines():
                if not line:
                    continue
                if line.startswith("??"):
                    untracked_files += 1
                else:
                    uncommitted_changes += 1

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
            "uncommitted_changes": uncommitted_changes,
            "untracked_files": untracked_files,
            "commits_ahead": commits_ahead,
            "commits_behind": commits_behind,
            "stale_push": stale_push,
            "last_commit_minutes_ago": last_commit_minutes_ago,
        }

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
            "commits_ahead": 0,
            "commits_behind": 0,
            "stale_push": False,
            "last_commit_minutes_ago": None,
        }
