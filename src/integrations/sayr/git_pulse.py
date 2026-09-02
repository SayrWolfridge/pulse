from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any


# The executor never sends note contents to a model. Ordinary large Markdown
# therefore remains safe to commit; this ceiling is only a repository-bloat
# guard, not a prompt/output limit.
MAX_NOTE_BYTES = 25 * 1024 * 1024
DEFAULT_RECEIPT_DIR = Path("~/.pulse/state/git-maintenance").expanduser()
TOPIC_MAP_PATH = "semantic-garden/sayr-thoughts/topic-map.json"
TOPIC_MAP_RESULT_KEYS = {
    "touch_count",
    "last_touched_at",
    "last_meaningful_result",
    "last_result_source",
    "last_result_at",
}


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
        lines = [
            "GIT ACTION",
            f"- kind: {self.kind}",
            f"- repo_name: {self.repo_name}",
            f"- repo_path: {self.repo_path or '<missing repo_path>'}",
            f"- headline: {self.headline}",
        ]
        if self.dirty_files:
            lines.append("- files:")
            lines.extend(f"  - {path}" for path in self.dirty_files)
        if self.details:
            lines.append("- details:")
            lines.extend(f"  - {detail}" for detail in self.details)
        return "\n".join(lines)


@dataclass(frozen=True)
class GitMaintenanceResult:
    outcome: str
    repo_name: str | None
    repo_path: str | None
    resolves_drive: bool
    before_head: str | None
    commit_hash: str | None
    committed_files: list[str]
    remaining_files: list[str]
    summary: str
    receipt_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GitMaintenanceResult":
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})

    def as_message(self) -> str:
        lines = [
            "GIT RESULT",
            "- execution: deterministic_no_model",
            f"- outcome: {self.outcome}",
            f"- repo_name: {self.repo_name}",
            f"- repo_path: {self.repo_path}",
            f"- summary: {self.summary}",
        ]
        if self.commit_hash:
            lines.append(f"- commit: {self.commit_hash}")
        if self.committed_files:
            lines.append("- committed_files:")
            lines.extend(f"  - {path}" for path in self.committed_files)
        if self.remaining_files:
            lines.append("- remaining_files_requiring_review:")
            lines.extend(f"  - {path}" for path in self.remaining_files)
        lines.append(
            "- required_action_by_sayr: report_this_result_only; do_not_run_git_or_spawn_subagents"
        )
        return "\n".join(lines)


def _run_git(repo_path: str, args: list[str], *, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo_path, check=False, capture_output=True,
        text=True, timeout=timeout,
    )


def _git(repo_path: str, args: list[str]) -> str:
    result = _run_git(repo_path, args)
    return result.stdout if result.returncode == 0 else ""


def _status_entries(repo_path: str) -> list[tuple[str, str]]:
    result = _run_git(repo_path, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if result.returncode != 0:
        return []
    entries: list[tuple[str, str]] = []
    chunks = result.stdout.split("\0")
    index = 0
    while index < len(chunks):
        chunk = chunks[index]
        index += 1
        if not chunk or len(chunk) < 4:
            continue
        status, path = chunk[:2], chunk[3:]
        if status[0] in {"R", "C"} and index < len(chunks):
            path = chunks[index]
            index += 1
        entries.append((status, path))
    return entries


def _parse_porcelain(raw: str) -> list[str]:
    files: list[str] = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip('"'))
    return files


def _ahead_behind(repo_path: str, fallback_ahead: int, fallback_behind: int) -> tuple[int, int]:
    parts = _git(repo_path, ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"]).split()
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
            "blocked", context.get("repo_name"), repo_path,
            "Git drive has no valid repo_path", [], [],
            int(context.get("commits_ahead") or 0), int(context.get("commits_behind") or 0),
        )
    repo_probe = _run_git(str(repo_path), ["rev-parse", "--is-inside-work-tree"])
    status_probe = _run_git(str(repo_path), ["status", "--porcelain=v1"])
    if (
        repo_probe.returncode != 0
        or repo_probe.stdout.strip() != "true"
        or status_probe.returncode != 0
    ):
        return GitPulseAction(
            "blocked", context.get("repo_name"), str(repo_path),
            "Git preflight could not verify repository state", [], [],
            int(context.get("commits_ahead") or 0), int(context.get("commits_behind") or 0),
        )
    dirty_files = _parse_porcelain(status_probe.stdout)
    ahead, behind = _ahead_behind(
        str(repo_path), int(context.get("commits_ahead") or 0),
        int(context.get("commits_behind") or 0),
    )
    if dirty_files:
        return GitPulseAction(
            "commit_needed", context.get("repo_name"), str(repo_path),
            "ЗАКОММИТЬ ВОТ ЭТО РЕПО", [], dirty_files, ahead, behind,
        )
    if ahead > 0:
        return GitPulseAction(
            "push_pending", context.get("repo_name"), str(repo_path),
            f"LOCAL COMMITS NOT PUSHED: {ahead}",
            ["Do not push autonomously"], [], ahead, behind,
        )
    if behind > 0:
        return GitPulseAction(
            "pull_or_rebase_pending", context.get("repo_name"), str(repo_path),
            f"REMOTE COMMITS AVAILABLE: {behind}",
            ["Do not pull/rebase autonomously"], [], ahead, behind,
        )
    return GitPulseAction(
        "clean", context.get("repo_name"), str(repo_path),
        "Git repo is clean; no agent wake needed", [], [], ahead, behind,
    )


def _is_safe_addition(repo_name: str | None, repo_path: Path, relative_path: str) -> bool:
    posix = PurePosixPath(relative_path.replace("\\", "/"))
    if posix.is_absolute() or ".." in posix.parts or posix.suffix.lower() != ".md":
        return False
    if any(part.startswith(".") for part in posix.parts):
        return False
    if repo_name == "workspace":
        allowed = (
            posix.parent == PurePosixPath("memory")
            or PurePosixPath("memory/dreaming") in posix.parents
            or PurePosixPath("semantic-garden/sayr-thoughts") in posix.parents
        )
        if not allowed:
            return False
    elif repo_name != "obsidian":
        return False
    full_path = repo_path.joinpath(*posix.parts)
    try:
        stat = full_path.lstat()
        if (
            full_path.is_symlink()
            or not full_path.is_file()
            or stat.st_size <= 0
            or stat.st_size > MAX_NOTE_BYTES
        ):
            return False
        # Read only the binary probe, never the full note. Trailing whitespace
        # is intentionally preserved: in Markdown two trailing spaces can be
        # meaningful and must not create a question or a silent rewrite.
        with full_path.open("rb") as stream:
            return b"\0" not in stream.read(8192)
    except OSError:
        return False


def _note_result_text(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def _safe_topic_map_companion(
    repo_name: str | None,
    repo_path: Path,
    eligible_notes: list[str],
) -> str | None:
    if repo_name != "workspace":
        return None
    note_texts: dict[str, str] = {}
    garden = PurePosixPath("semantic-garden/sayr-thoughts")
    for relative_path in eligible_notes:
        posix = PurePosixPath(relative_path.replace("\\", "/"))
        if garden not in posix.parents:
            continue
        text = _note_result_text(repo_path.joinpath(*posix.parts))
        if text is None:
            return None
        note_texts[posix.as_posix()] = text
    if not note_texts:
        return None

    baseline = _run_git(repo_path.as_posix(), ["show", f"HEAD:{TOPIC_MAP_PATH}"])
    if baseline.returncode != 0:
        return None
    try:
        before = json.loads(baseline.stdout)
        after = json.loads((repo_path / TOPIC_MAP_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    if {key: value for key, value in before.items() if key != "families"} != {
        key: value for key, value in after.items() if key != "families"
    }:
        return None
    before_families = before.get("families")
    after_families = after.get("families")
    if not isinstance(before_families, list) or not isinstance(after_families, list):
        return None
    if len(before_families) != len(after_families):
        return None

    changed = 0
    used_sources: set[str] = set()
    for old_family, new_family in zip(before_families, after_families, strict=True):
        if old_family == new_family:
            continue
        if not isinstance(old_family, dict) or not isinstance(new_family, dict):
            return None
        if old_family.get("id") != new_family.get("id"):
            return None
        changed_keys = {
            key for key in old_family.keys() | new_family.keys()
            if old_family.get(key) != new_family.get(key)
        }
        if changed_keys != TOPIC_MAP_RESULT_KEYS:
            return None
        old_count = old_family.get("touch_count")
        if not isinstance(old_count, int) or new_family.get("touch_count") != old_count + 1:
            return None
        source = new_family.get("last_result_source")
        if not isinstance(source, str) or source not in note_texts or source in used_sources:
            return None
        if new_family.get("last_meaningful_result") != note_texts[source]:
            return None
        timestamp = new_family.get("last_result_at")
        if not isinstance(timestamp, str) or not timestamp:
            return None
        if new_family.get("last_touched_at") != timestamp:
            return None
        used_sources.add(source)
        changed += 1
    return TOPIC_MAP_PATH if changed else None


def _write_receipt(result: GitMaintenanceResult, receipt_dir: Path) -> GitMaintenanceResult:
    receipt_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    final_path = receipt_dir / (
        f"{stamp}-{result.repo_name or 'unknown'}-{os.getpid()}-{time.time_ns()}.json"
    )
    temp_path = final_path.with_suffix(".tmp")
    payload = result.as_dict()
    payload["receipt_path"] = str(final_path)
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(final_path)
    return GitMaintenanceResult.from_dict(payload)


def execute_git_maintenance(decision: Any, *, receipt_dir: Path | None = None) -> GitMaintenanceResult | None:
    action = analyze_git_drive(decision)
    if action is None:
        return None
    receipts = receipt_dir or DEFAULT_RECEIPT_DIR
    repo_path = Path(action.repo_path) if action.repo_path else None

    def finish(**kwargs: Any) -> GitMaintenanceResult:
        return _write_receipt(GitMaintenanceResult(
            repo_name=action.repo_name,
            repo_path=action.repo_path,
            before_head=kwargs.pop("before_head", None),
            commit_hash=kwargs.pop("commit_hash", None),
            committed_files=kwargs.pop("committed_files", []),
            remaining_files=kwargs.pop("remaining_files", action.dirty_files),
            receipt_path=None,
            **kwargs,
        ), receipts)

    if action.kind == "clean":
        return finish(outcome="no_action", resolves_drive=True, remaining_files=[],
                      summary="Repository is already clean")
    if action.kind != "commit_needed" or repo_path is None:
        return finish(outcome="blocked", resolves_drive=False,
                      summary=f"Automatic Git is not allowed for {action.kind}")

    entries = _status_entries(str(repo_path))
    staged = [path for status, path in entries if status != "??" and status[0] != " "]
    if staged:
        return finish(
            outcome="blocked", resolves_drive=False,
            remaining_files=[path for _, path in entries],
            summary="Repository already has staged changes; Pulse left the index untouched",
        )
    eligible = sorted(
        path for status, path in entries
        if status == "??" and _is_safe_addition(action.repo_name, repo_path, path)
    )
    if not eligible:
        return finish(
            outcome="no_safe_slice", resolves_drive=False,
            remaining_files=[path for _, path in entries],
            summary="No non-empty additive Markdown notes matched the safe allowlist",
        )
    status_by_path = {path: status for status, path in entries}
    companions: list[str] = []
    if status_by_path.get(TOPIC_MAP_PATH) == " M":
        companion = _safe_topic_map_companion(action.repo_name, repo_path, eligible)
        if companion:
            companions.append(companion)
    stage_paths = sorted([*eligible, *companions])

    before_head = _git(str(repo_path), ["rev-parse", "HEAD"]).strip()
    if not before_head:
        return finish(outcome="blocked", resolves_drive=False,
                      remaining_files=[path for _, path in entries],
                      summary="Repository has no stable HEAD")
    if _git(str(repo_path), ["rev-parse", "HEAD"]).strip() != before_head:
        return finish(outcome="blocked", resolves_drive=False, before_head=before_head,
                      remaining_files=[path for _, path in entries],
                      summary="HEAD changed during preflight")

    add = _run_git(str(repo_path), ["add", "--", *stage_paths])
    if add.returncode != 0:
        return finish(outcome="failed", resolves_drive=False, before_head=before_head,
                      remaining_files=[path for _, path in _status_entries(str(repo_path))],
                      summary="git add failed; no commit was created")
    after_add = _status_entries(str(repo_path))
    staged_now = sorted(
        path for status, path in after_add
        if status != "??" and status[0] != " "
    )
    staged_states = {path: status for status, path in after_add if path in stage_paths}
    expected_states = {
        **{path: "A " for path in eligible},
        **{path: "M " for path in companions},
    }
    safe_index = staged_now == stage_paths and staged_states == expected_states
    head_unchanged = _git(str(repo_path), ["rev-parse", "HEAD"]).strip() == before_head
    if not safe_index or not head_unchanged:
        _run_git(str(repo_path), ["restore", "--staged", "--", *stage_paths])
        return finish(outcome="blocked", resolves_drive=False, before_head=before_head,
                      remaining_files=[path for _, path in _status_entries(str(repo_path))],
                      summary="Repository changed during staging; Pulse rolled back its index entries")

    commit = _run_git(
        str(repo_path),
        [
            "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgSign=false",
            "commit", "-m", "chore(notes): save Pulse additions",
        ],
        timeout=30,
    )
    if commit.returncode != 0:
        _run_git(str(repo_path), ["restore", "--staged", "--", *stage_paths])
        return finish(outcome="failed", resolves_drive=False, before_head=before_head,
                      remaining_files=[path for _, path in _status_entries(str(repo_path))],
                      summary="git commit failed; Pulse rolled back its index entries")

    commit_hash = _git(str(repo_path), ["rev-parse", "HEAD"]).strip()
    if not commit_hash or commit_hash == before_head:
        return finish(
            outcome="failed",
            resolves_drive=False,
            before_head=before_head,
            remaining_files=[path for _, path in _status_entries(str(repo_path))],
            summary="git commit returned without a verifiable new HEAD",
        )
    remaining = [path for _, path in _status_entries(str(repo_path))]
    return finish(
        outcome="committed" if not remaining else "committed_partial",
        resolves_drive=not remaining,
        before_head=before_head,
        commit_hash=commit_hash,
        committed_files=stage_paths,
        remaining_files=remaining,
        summary=("Safe additive notes committed; repository is clean" if not remaining
                 else "Safe additive notes committed; disallowed changes remain for review"),
    )
