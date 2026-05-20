"""
Default Integration — minimal, works with any OpenClaw agent.

Sends a simple trigger message with drive info. No assumptions about
CORTEX.md, hippocampus, or any specific agent architecture.
"""

from pathlib import Path
import re

from pulse.src.integrations import Integration


CORTEX_PULSE_PATH = Path("/home/lisa/.openclaw/workspace/pulse/CORTEX-pulse.md")


def _load_drive_protocol(drive_name: str) -> str | None:
    """Load the drive-specific protocol block from Lisa's Pulse cortex."""
    try:
        text = CORTEX_PULSE_PATH.read_text(encoding="utf-8")
    except OSError:
        return None

    headings = [drive_name]
    if drive_name.endswith("_git"):
        headings = ["*_git", drive_name]

    for heading in headings:
        pattern = re.compile(
            rf"^##+\s+(?:\d+\.\s+)?`drive = {re.escape(heading)}`.*?(?=^---\s*$|^##+\s)",
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(text)
        if match:
            block = match.group(0).rstrip()
            if heading != drive_name:
                block = block.replace("`drive = *_git`", f"`drive = {drive_name}`")
                block = block.replace("любого drive", f"drive `{drive_name}`")
            return block
    return None


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
        drive_protocol = _load_drive_protocol(drive_name) if drive_name else None
        if drive_protocol:
            parts.extend([
                "",
                "Drive-specific protocol:",
                drive_protocol,
            ])
        elif drive_name:
            parts.append(
                f"Drive-specific protocol missing for {drive_name}. "
                f"Tell Lisa in the reply: 'не подтянулась инструкция для {drive_name}, "
                "надо поправить подтягивание, когда Лиса придёт' — and stop."
            )
        else:
            parts.append(
                "Check if there's something worth doing for this drive. "
                "If сейчас не время, ресурса нет, or the drive-specific block says no visible action is needed — "
                "briefly tell Lisa what you reviewed/did and why you are stopping; do not invent work."
            )

        return "\n".join(parts)
