"""
Pulse v2 Runtime — HypostasRuntime
===================================
Always-running cognition layer for Iris.

Modules:
    StateEngine   — 30s autosave of full cognitive state
    ContextEngine — Hot/warm/cold/relationship memory tiers
    ThoughtLoop   — Background cognition using iris-70b (local, $0)
    RuntimeBridge — Connects new runtime to existing Pulse daemon hooks

Entry point: python -m pulse.runtime
"""

from .state_engine import StateEngine
from .context_engine import ContextEngine

__all__ = ["StateEngine", "ContextEngine"]
