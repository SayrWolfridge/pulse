from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GitPulseAction:
    kind: str
    repo_name: str | None
    repo_path: str | None
    headline: str
    details: list[str]
    dirty_files: list[str]
    ahead: int
    behind: int

    def as_message(self) -> str:
        repo_path = self.repo_path or "<missing repo_path>"
        lines = [
            "GIT ACTION",
            f"- kind: {self.kind}",
            f"- repo_name: {self.repo_name}",
            f"- repo_path: {repo_path}",
            f"- headline: {self.headline}",
        ]
        if self.kind == "commit_needed":
            lines.extend([
                "- required_action: commit_this_repo",
                f"- target_repo: {repo_path}",
            ])
        if self.dirty_files:
            lines.append("- files:")
            lines.extend(f"  - {path}" for path in self.dirty_files)
        if self.details:
            lines.append("- details:")
            lines.extend(f"  - {detail}" for detail in self.details)
        lines.append("Use repo_path as the target repository for this git action.")
        return "\n".join(lines)


def _git(repo_path: str, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _parse_porcelain(raw: str) -> list[str]:
    files: list[str] = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip('\"'))
    return files


def _ahead_behind(repo_path: str, fallback_ahead: int, fallback_behind: int) -> tuple[int, int]:
    raw = _git(repo_path, ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"]).strip()
    parts = raw.split()
    if len(parts) == 2:
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            pass
    return fallback_ahead, fallback_behind


def analyze_git_drive(decision: Any) -> GitPulseAction | None:
    drive = getattr(decision, "top_drive", None)
    if not drive or not str(getattr(drive, "name", "")).endswith("_git"):
        return None

    context = getattr(drive, "source_data", {}).get("git")
    if not isinstance(context, dict):
        return None

    repo_path = context.get("repo_path")
    if not repo_path or not Path(str(repo_path)).is_dir():
        return GitPulseAction(
            kind="blocked",
            repo_name=context.get("repo_name"),
            repo_path=repo_path,
            headline="Git drive has no valid repo_path; cannot choose action safely",
            details=["Fix git sensor/drive context before waking the agent"],
            dirty_files=[],
            ahead=int(context.get("commits_ahead") or 0),
            behind=int(context.get("commits_behind") or 0),
        )

    status = _git(str(repo_path), ["status", "--porcelain=v1"])
    dirty_files = _parse_porcelain(status)
    ahead, behind = _ahead_behind(
        str(repo_path),
        int(context.get("commits_ahead") or 0),
        int(context.get("commits_behind") or 0),
    )

    if dirty_files:
        return GitPulseAction(
            kind="commit_needed",
            repo_name=context.get("repo_name"),
            repo_path=str(repo_path),
            headline="ЗАКОММИТЬ ВОТ ЭТО РЕПО",
            details=[
                f"Work in {repo_path}.",
                f"Run git -C {repo_path} status --short.",
                f"Review git -C {repo_path} diff.",
                f"If safe, stage and commit local changes in {repo_path}.",
                f"Verify git -C {repo_path} status --short after commit.",
            ],
            dirty_files=dirty_files,
            ahead=ahead,
            behind=behind,
        )

    if ahead > 0:
        return GitPulseAction(
            kind="push_pending",
            repo_name=context.get("repo_name"),
            repo_path=str(repo_path),
            headline=f"LOCAL COMMITS NOT PUSHED: {ahead}",
            details=["Do not push autonomously; report or ask Lisa if external push is wanted"],
            dirty_files=[],
            ahead=ahead,
            behind=behind,
        )

    if behind > 0:
        return GitPulseAction(
            kind="pull_or_rebase_pending",
            repo_name=context.get("repo_name"),
            repo_path=str(repo_path),
            headline=f"REMOTE COMMITS AVAILABLE: {behind}",
            details=["Do not pull/rebase autonomously; plan update separately"],
            dirty_files=[],
            ahead=ahead,
            behind=behind,
        )

    return GitPulseAction(
        kind="clean",
        repo_name=context.get("repo_name"),
        repo_path=str(repo_path),
        headline="Git repo is clean; no agent wake needed",
        details=[],
        dirty_files=[],
        ahead=ahead,
        behind=behind,
    )
