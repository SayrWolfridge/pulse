"""Logos task schema — the shape of work in the Hypostas backlog."""

import time
import uuid
from dataclasses import dataclass, field, asdict


# Naming philosophy — symbolic constants for task lifecycle stages
CANON = "backlog"       # the Canon — what must be done
PLEROMA = "backlog"     # the Pleroma — the fullness/potential
AGORA = "review"        # the Agora — where decisions are made together
ARCHIVE = "done"        # completed work

VALID_STATUSES = {"backlog", "in_progress", "review", "done", "blocked"}
VALID_PROJECTS = {"gnosis", "anima", "aether", "pulse", "soma", "logos"}
VALID_AGENTS = {"mira", "vera", "lyra", "sage", "iris"}


@dataclass
class Task:
    title: str
    description: str
    project: str
    agent: str = "mira"
    status: str = "backlog"
    priority: int = 3
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    parent_id: str | None = None
    tags: list[str] = field(default_factory=list)
    spec: str = ""
    output: str | None = None
    review_notes: str | None = None
    requires_human: bool = False
    deploy_ready: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        # Filter to only known fields
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})
