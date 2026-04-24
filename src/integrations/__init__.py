"""
Integrations — pluggable post-trigger behavior.

An integration defines:
1. How to build the trigger message sent to the agent
2. What drives map to (optional workspace-specific source reading)
3. How feedback flows back

Core Pulse (drives, sensors, evaluator, webhook) is generic.
Integrations customize what happens after a trigger decision.
"""

from abc import ABC, abstractmethod
from typing import Optional


class Integration(ABC):
    """Base class for Pulse integrations."""

    name: str = "base"

    @abstractmethod
    def build_trigger_message(self, decision, config) -> str:
        """Build the message sent to OpenClaw when a trigger fires.

        Args:
            decision: TriggerDecision with reason, top_drive, pressure, etc.
            config: PulseConfig for access to prefix and other settings.

        Returns:
            String message to send via webhook.
        """
        ...

    def suppress_trigger(self, decision, config) -> Optional[dict]:
        """Optionally suppress an already-positive trigger decision before webhook.

        Integrations use this for deterministic preflight: if a drive can be
        resolved mechanically or has no human-visible work, return a dict with
        optional feedback payload instead of waking the agent/model.
        Return None/False to continue with the normal webhook trigger.
        """
        return None

    def on_startup(self, daemon) -> None:
        """Called when daemon starts. Override to initialize integration-specific resources."""
        pass

    def on_shutdown(self, daemon) -> None:
        """Called when daemon stops."""
        pass
