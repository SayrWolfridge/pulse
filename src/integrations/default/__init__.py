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

        if decision.sensor_context:
            parts.append(f"Suggested focus: {decision.sensor_context}")

        parts.append(
            "Check if there's something worth doing for this drive. "
            "If сейчас не время, ресурса нет, or the drive-specific block says no visible action is needed — "
            "say so briefly and do not invent work."
        )

        return "\n".join(parts)
