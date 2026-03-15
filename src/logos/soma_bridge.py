"""Soma bridge — connects Logos backlog pressure to the Soma drive system."""

import logging
import re
import time

from pulse.src.logos.schemas import Task
from pulse.src.logos.store import LogosStore

logger = logging.getLogger("pulse.logos.soma_bridge")


class SomaBridge:
    """Bridge between the Logos task engine and Soma drives."""

    def __init__(self, store: LogosStore | None = None):
        self.store = store or LogosStore()

    def ingest_spec(
        self, spec_text: str, project: str, agent: str = "mira"
    ) -> list[Task]:
        """Parse bullet/numbered lists from a spec into individual tasks."""
        tasks = []
        lines = spec_text.strip().splitlines()
        for line in lines:
            line = line.strip()
            # Match bullet points (-, *, •) or numbered lists (1., 2., etc.)
            match = re.match(r"^(?:[-*•]|\d+[.)]\s*)\s*(.+)$", line)
            if match:
                title = match.group(1).strip()
                if title:
                    task = Task(
                        title=title,
                        description="",
                        project=project,
                        agent=agent,
                        spec=spec_text,
                    )
                    self.store.create_task(task)
                    tasks.append(task)
        return tasks

    def review_output(
        self, task_id: str, output: str, store: LogosStore | None = None
    ) -> Task | None:
        """Mark task for review, flagging incomplete outputs."""
        s = store or self.store
        requires_human = (
            len(output.strip()) < 50
            or "TODO" in output
            or "blocked" in output.lower()
        )
        task = s.update_task(
            task_id,
            status="review",
            output=output,
            requires_human=requires_human,
        )
        if task and requires_human:
            logger.info(f"Task {task_id} flagged for human review: output may be incomplete")
        return task

    def get_logos_pressure(self, store: LogosStore | None = None) -> float:
        """Return 0.0-1.0 pressure based on overdue/blocked task density.

        High pressure when many tasks are blocked or high-priority backlog items
        are accumulating.
        """
        s = store or self.store
        all_tasks = s.list_tasks()
        if not all_tasks:
            return 0.0

        blocked = sum(1 for t in all_tasks if t.status == "blocked")
        critical_backlog = sum(
            1 for t in all_tasks
            if t.status == "backlog" and t.priority >= 4
        )
        total_backlog = sum(1 for t in all_tasks if t.status == "backlog")

        # Weighted pressure: blocked tasks are heavy, critical backlog matters
        pressure = (blocked * 0.15) + (critical_backlog * 0.08) + (total_backlog * 0.02)
        return min(1.0, max(0.0, pressure))
