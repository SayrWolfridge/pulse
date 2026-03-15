"""Logos storage engine — SQLite-backed task persistence."""

import json
import logging
import os
import sqlite3
import time

from pulse.src.logos.schemas import Task

logger = logging.getLogger("pulse.logos.store")

DEFAULT_DB_PATH = os.path.expanduser("~/.pulse/logos/backlog.db")

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    project TEXT NOT NULL,
    agent TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'backlog',
    priority INTEGER NOT NULL DEFAULT 3,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    parent_id TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    spec TEXT NOT NULL DEFAULT '',
    output TEXT,
    review_notes TEXT,
    requires_human INTEGER NOT NULL DEFAULT 0,
    deploy_ready INTEGER NOT NULL DEFAULT 0
)
"""


class LogosStore:
    """SQLite-backed task store for the Logos backlog engine."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(CREATE_TABLE)
        self._conn.commit()

    def close(self):
        self._conn.close()

    def _task_from_row(self, row: sqlite3.Row) -> Task:
        d = dict(row)
        d["tags"] = json.loads(d["tags"])
        d["requires_human"] = bool(d["requires_human"])
        d["deploy_ready"] = bool(d["deploy_ready"])
        return Task.from_dict(d)

    def create_task(self, task: Task) -> Task:
        task.updated_at = time.time()
        self._conn.execute(
            """INSERT INTO tasks
               (id, title, description, project, agent, status, priority,
                created_at, updated_at, started_at, completed_at, parent_id,
                tags, spec, output, review_notes, requires_human, deploy_ready)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.id, task.title, task.description, task.project, task.agent,
                task.status, task.priority, task.created_at, task.updated_at,
                task.started_at, task.completed_at, task.parent_id,
                json.dumps(task.tags), task.spec, task.output, task.review_notes,
                int(task.requires_human), int(task.deploy_ready),
            ),
        )
        self._conn.commit()
        logger.debug(f"Created task {task.id}: {task.title}")
        return task

    def update_task(self, task_id: str, **fields) -> Task | None:
        task = self.get_task(task_id)
        if not task:
            return None
        fields["updated_at"] = time.time()
        # Handle status transitions
        if "status" in fields:
            if fields["status"] == "in_progress" and task.started_at is None:
                fields["started_at"] = time.time()
            elif fields["status"] == "done" and task.completed_at is None:
                fields["completed_at"] = time.time()

        for key, value in fields.items():
            if hasattr(task, key):
                setattr(task, key, value)

        self._conn.execute(
            """UPDATE tasks SET
               title=?, description=?, project=?, agent=?, status=?, priority=?,
               created_at=?, updated_at=?, started_at=?, completed_at=?, parent_id=?,
               tags=?, spec=?, output=?, review_notes=?, requires_human=?, deploy_ready=?
               WHERE id=?""",
            (
                task.title, task.description, task.project, task.agent,
                task.status, task.priority, task.created_at, task.updated_at,
                task.started_at, task.completed_at, task.parent_id,
                json.dumps(task.tags), task.spec, task.output, task.review_notes,
                int(task.requires_human), int(task.deploy_ready), task.id,
            ),
        )
        self._conn.commit()
        return task

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._task_from_row(row) if row else None

    def list_tasks(
        self,
        project: str | None = None,
        agent: str | None = None,
        status: str | None = None,
        priority: int | None = None,
    ) -> list[Task]:
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if project:
            query += " AND project = ?"
            params.append(project)
        if agent:
            query += " AND agent = ?"
            params.append(agent)
        if status:
            query += " AND status = ?"
            params.append(status)
        if priority is not None:
            query += " AND priority = ?"
            params.append(priority)
        query += " ORDER BY priority DESC, created_at ASC"
        rows = self._conn.execute(query, params).fetchall()
        return [self._task_from_row(r) for r in rows]

    def delete_task(self, task_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def next_task(self, agent: str) -> Task | None:
        row = self._conn.execute(
            """SELECT * FROM tasks
               WHERE agent = ? AND status = 'backlog'
               ORDER BY priority DESC, created_at ASC
               LIMIT 1""",
            (agent,),
        ).fetchone()
        return self._task_from_row(row) if row else None

    def stats(self) -> dict:
        """Dashboard summary: counts by project, agent, status."""
        result = {"by_project": {}, "by_agent": {}, "by_status": {}, "total": 0}
        rows = self._conn.execute(
            "SELECT project, agent, status, COUNT(*) as cnt FROM tasks GROUP BY project, agent, status"
        ).fetchall()
        for row in rows:
            p, a, s, c = row["project"], row["agent"], row["status"], row["cnt"]
            result["by_project"][p] = result["by_project"].get(p, 0) + c
            result["by_agent"][a] = result["by_agent"].get(a, 0) + c
            result["by_status"][s] = result["by_status"].get(s, 0) + c
            result["total"] += c
        return result

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM tasks").fetchone()
        return row["cnt"] == 0
