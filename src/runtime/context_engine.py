"""
ContextEngine — Pulse v2
=========================
Hot/warm/cold/relationship memory tiers.

Day 2 implementation (coming next).
This stub allows StateEngine to be tested independently.
"""

# Full implementation: Day 2 sprint
# See PULSE_V2_PHASE1_SPRINT.md for spec


class ContextEngine:
    """Stub — Day 2 implementation."""

    def __init__(self, state_dir=None):
        self.state_dir = state_dir

    def log_event(self, event: dict) -> None:
        pass  # Day 2

    def get_recent_context(self, hours: int = 2) -> list:
        return []  # Day 2

    def get_relationship(self, person: str) -> dict:
        return {}  # Day 2

    def update_relationship(self, person: str, event: dict) -> None:
        pass  # Day 2
