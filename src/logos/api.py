"""Logos HTTP API — aiohttp routes for task management."""

import json
import logging
from aiohttp import web

from pulse.src.logos.store import LogosStore
from pulse.src.logos.messages import MessageStore

logger = logging.getLogger("pulse.logos.api")


class LogosAPI:
    """HTTP route handlers for the Logos backlog engine.

    Call register_routes(app) to mount /logos/* on an aiohttp application.
    """

    def __init__(self, store: LogosStore | None = None, message_store: MessageStore | None = None):
        self.store = store or LogosStore()
        self.messages = message_store or MessageStore(db_path=self.store.db_path)

    def register_routes(self, app: web.Application):
        """Mount all Logos routes on an aiohttp app."""
        app.router.add_get("/logos/tasks", self._list_tasks)
        app.router.add_post("/logos/tasks", self._create_task)
        app.router.add_get("/logos/tasks/{id}", self._get_task)
        app.router.add_patch("/logos/tasks/{id}", self._update_task)
        app.router.add_delete("/logos/tasks/{id}", self._delete_task)
        app.router.add_get("/logos/next/{agent}", self._next_task)
        app.router.add_get("/logos/stats", self._stats)
        # Messaging routes
        app.router.add_get("/logos/messages/all", self._all_messages)
        app.router.add_get("/logos/messages/thread/{thread_id}", self._get_thread)
        app.router.add_post("/logos/messages", self._send_message)
        app.router.add_get("/logos/messages/{agent}", self._get_inbox)
        app.router.add_patch("/logos/messages/{id}/read", self._mark_read)

    async def _list_tasks(self, request: web.Request) -> web.Response:
        project = request.query.get("project")
        agent = request.query.get("agent")
        status = request.query.get("status")
        tasks = self.store.list_tasks(project=project, agent=agent, status=status)
        return web.json_response([t.to_dict() for t in tasks])

    async def _create_task(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        if not data.get("title") or not data.get("project"):
            return web.json_response(
                {"error": "title and project are required"}, status=400
            )

        from pulse.src.logos.schemas import Task
        task = Task.from_dict({
            "title": data["title"],
            "description": data.get("description", ""),
            "project": data["project"],
            "agent": data.get("agent", "mira"),
            "status": data.get("status", "backlog"),
            "priority": data.get("priority", 3),
            "tags": data.get("tags", []),
            "spec": data.get("spec", ""),
            "requires_human": data.get("requires_human", False),
            "parent_id": data.get("parent_id"),
        })
        self.store.create_task(task)
        return web.json_response(task.to_dict(), status=201)

    async def _get_task(self, request: web.Request) -> web.Response:
        task_id = request.match_info["id"]
        task = self.store.get_task(task_id)
        if not task:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(task.to_dict())

    async def _update_task(self, request: web.Request) -> web.Response:
        task_id = request.match_info["id"]
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        task = self.store.update_task(task_id, **data)
        if not task:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(task.to_dict())

    async def _delete_task(self, request: web.Request) -> web.Response:
        task_id = request.match_info["id"]
        deleted = self.store.delete_task(task_id)
        if not deleted:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"status": "deleted"})

    async def _next_task(self, request: web.Request) -> web.Response:
        agent = request.match_info["agent"]
        task = self.store.next_task(agent)
        if not task:
            return web.json_response({"error": "no tasks available"}, status=404)
        return web.json_response(task.to_dict())

    async def _stats(self, request: web.Request) -> web.Response:
        return web.json_response(self.store.stats())

    # --- Messaging endpoints ---

    async def _send_message(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        required = ("from", "to", "subject", "body")
        if not all(data.get(k) for k in required):
            return web.json_response({"error": "from, to, subject, body are required"}, status=400)
        msg_id = self.messages.send_message(
            from_agent=data["from"],
            to_agent=data["to"],
            subject=data["subject"],
            body=data["body"],
            thread_id=data.get("thread_id"),
            priority=data.get("priority", 3),
        )
        return web.json_response({"id": msg_id, "status": "sent"}, status=201)

    async def _get_inbox(self, request: web.Request) -> web.Response:
        agent = request.match_info["agent"]
        include_read = request.query.get("all", "").lower() in ("true", "1")
        msgs = self.messages.get_inbox(agent, include_read=include_read)
        return web.json_response(msgs)

    async def _mark_read(self, request: web.Request) -> web.Response:
        msg_id = request.match_info["id"]
        if self.messages.mark_read(msg_id):
            return web.json_response({"status": "marked_read"})
        return web.json_response({"error": "not found"}, status=404)

    async def _get_thread(self, request: web.Request) -> web.Response:
        thread_id = request.match_info["thread_id"]
        msgs = self.messages.get_thread(thread_id)
        return web.json_response(msgs)

    async def _all_messages(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50
        msgs = self.messages.get_all_messages(limit=limit)
        return web.json_response(msgs)
