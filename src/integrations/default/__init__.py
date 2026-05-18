"""
Default Integration — minimal, works with any OpenClaw agent.

Sends a simple trigger message with drive info. No assumptions about
CORTEX.md, hippocampus, or any specific agent architecture.
"""

from pulse.src.integrations import Integration


class DefaultIntegration(Integration):
    """Generic integration that works with any OpenClaw bot."""

    name = "default"

    def build_trigger_message(self, decision, config) -> str:
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

        git_context = None
        if decision.top_drive:
            git_context = decision.top_drive.source_data.get("git")
        if git_context:
            parts.extend([
                "Git repo contract:",
                f"- repo_name: {git_context.get('repo_name')}",
                f"- repo_path: {git_context.get('repo_path')}",
                f"- drive: {decision.top_drive.name}",
                f"- reason: {', '.join(git_context.get('reasons') or [])}",
                f"- dirty: {git_context.get('pressure_dirty')}",
                f"- stale_push: {git_context.get('stale_push')}",
                f"- commits_ahead: {git_context.get('commits_ahead')}",
                f"- commits_behind: {git_context.get('commits_behind')}",
                "Use repo_path for git checks; do not infer the repository from the drive name.",
            ])
        elif decision.sensor_context:
            parts.append(f"Suggested focus: {decision.sensor_context}")

        drive_name = decision.top_drive.name if decision.top_drive else ""
        if drive_name.endswith("_git"):
            parts.append(
                "Drive contract: git. "
                "Default autonomous action is to bring the relevant repo to a clean local git state: "
                "use repo_path from the Git repo contract when present; otherwise use the drive-specific repo from CORTEX/prompt context, "
                "run git status, inspect enough to catch secrets, suspicious deletes/data loss, wrong repo, conflicts, prod/config risk, or out-of-scope files, "
                "run the smallest meaningful gate (at least git diff --check for text changes; tests/lint only when appropriate), "
                "then commit safe local changes with a clear message and verify git status --short. "
                "Do not stop with an uncertainty report just because the changes are personal notes, diary, memory, docs, or already-made local work; local git sync is the point of *_git drives. "
                "Ask Lisa only for a real blocker: secrets, suspicious deletion/data loss, wrong repo, conflicting/unclear provenance, live prod/runtime/config risk, or any push/publish/external effect. "
                "Never push unless Lisa explicitly asks."
            )
        else:
            parts.append(
                "Check if there's something worth doing for this drive. "
                "If сейчас не время, ресурса нет, or the drive-specific block says no visible action is needed — "
                "briefly tell Lisa what you reviewed/did and why you are stopping; do not invent work."
            )

        return "\n".join(parts)
