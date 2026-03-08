from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass
class InstinctTrigger:
    drives: dict[str, str]
    context: dict[str, Any]


@dataclass
class InstinctOutput:
    log: bool = True
    discord: Optional[str] = None
    signal: bool = False


@dataclass
class Instinct:
    name: str
    description: str
    version: str
    enabled: bool
    triggers: InstinctTrigger
    cooldown_minutes: int
    timeout_seconds: int
    output: InstinctOutput
    script: str
    body: str
    path: Path


@dataclass
class InstinctResult:
    instinct_name: str
    success: bool
    output: str
    error: Optional[str]
    duration_seconds: float
    fired_at: float
