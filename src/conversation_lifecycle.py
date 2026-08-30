"""Durable lifecycle for Pulse conversation candidates.

Webhook admission, visible agent output, Lisa's decision, and drive-quality
feedback are separate events. This module owns only the growth candidate's
delivery/lifecycle bookkeeping; it never changes learner pressure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


GROWTH_CALLBACK_PREFIX = "pulse.conversation.growth:"
GROWTH_ITEM_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
DISPATCH_PENDING_SECONDS = 2 * 60 * 60
DELIVERY_RETRY_SECONDS = 30 * 60


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso_seconds(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def growth_callback_kind(item_id: str) -> str:
    if not GROWTH_ITEM_ID.fullmatch(item_id):
        raise ValueError(f"unsafe growth conversation id: {item_id!r}")
    return f"{GROWTH_CALLBACK_PREFIX}{item_id}"


def growth_id_from_callback_kind(kind: object) -> str | None:
    if not isinstance(kind, str) or not kind.startswith(GROWTH_CALLBACK_PREFIX):
        return None
    item_id = kind[len(GROWTH_CALLBACK_PREFIX):]
    return item_id if GROWTH_ITEM_ID.fullmatch(item_id) else None


def read_growth_material(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError(f"{path} must contain an object with items[]")
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _find_item(data: dict, item_id: str) -> dict | None:
    return next(
        (
            item
            for item in data.get("items", [])
            if isinstance(item, dict) and item.get("id") == item_id
        ),
        None,
    )


def mark_growth_dispatch_pending(
    path: Path,
    item_id: str,
    *,
    now: datetime | None = None,
) -> dict:
    """Record HTTP admission without pretending visible output exists."""
    now = now or now_local()
    data = read_growth_material(path)
    item = _find_item(data, item_id)
    if item is None:
        return {"changed": False, "reason": "item-not-found", "id": item_id}
    if item.get("status") not in {"candidate", "later"}:
        return {
            "changed": False,
            "reason": f"status-{item.get('status')}-not-dispatchable",
            "id": item_id,
        }

    item["dispatch_accepted_at"] = iso_seconds(now)
    item["dispatch_pending_until"] = iso_seconds(
        now + timedelta(seconds=DISPATCH_PENDING_SECONDS)
    )
    item.pop("delivery_retry_after", None)
    _write_atomic(path, data)
    return {
        "changed": True,
        "status": item.get("status"),
        "id": item_id,
        "dispatch_pending_until": item["dispatch_pending_until"],
    }


def mark_growth_terminal_result(
    path: Path,
    item_id: str,
    *,
    status: str,
    run_id: object = None,
    output_text: str = "",
    now: datetime | None = None,
) -> dict:
    """Move to awaiting_lisa only after a terminal result with visible text."""
    now = now or now_local()
    data = read_growth_material(path)
    item = _find_item(data, item_id)
    if item is None:
        return {"changed": False, "reason": "item-not-found", "id": item_id}

    run = str(run_id or "")[:160]
    if status != "ok":
        if item.get("status") not in {"candidate", "later"}:
            return {
                "changed": False,
                "reason": f"status-{item.get('status')}-ignores-failed-result",
                "id": item_id,
            }
        item["last_delivery_status"] = str(status or "unknown")[:80]
        item["last_delivery_run_id"] = run or None
        item["last_delivery_failed_at"] = iso_seconds(now)
        item["delivery_retry_after"] = iso_seconds(
            now + timedelta(seconds=DELIVERY_RETRY_SECONDS)
        )
        item.pop("dispatch_pending_until", None)
        _write_atomic(path, data)
        return {
            "changed": True,
            "status": item.get("status"),
            "id": item_id,
            "delivery": "failed",
        }

    visible = output_text.strip()
    if not visible:
        return {"changed": False, "reason": "missing-visible-output", "id": item_id}

    if item.get("status") == "awaiting_lisa":
        same_run = not run or item.get("offered_run_id") == run
        return {
            "changed": False,
            "reason": "already-awaiting-lisa" if same_run else "stale-terminal-result",
            "id": item_id,
            "status": "awaiting_lisa",
        }
    if item.get("status") not in {"candidate", "later"}:
        return {
            "changed": False,
            "reason": f"status-{item.get('status')}-not-offerable",
            "id": item_id,
        }

    previous_status = item.get("status")
    item["status"] = "awaiting_lisa"
    item["offered_at"] = iso_seconds(now)
    item["offered_run_id"] = run or None
    item["visible_output_sha256"] = hashlib.sha256(
        visible.encode("utf-8")
    ).hexdigest()
    item["offered_from_status"] = previous_status
    item.pop("dispatch_pending_until", None)
    item.pop("delivery_retry_after", None)
    _write_atomic(path, data)
    return {
        "changed": True,
        "status": "awaiting_lisa",
        "id": item_id,
        "offered_at": item["offered_at"],
    }
