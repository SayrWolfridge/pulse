import json
from datetime import datetime

from pulse.src.conversation_lifecycle import (
    growth_callback_kind,
    growth_id_from_callback_kind,
    mark_growth_dispatch_pending,
    mark_growth_terminal_result,
)


def _write(path, items):
    path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")


def _item(path):
    return json.loads(path.read_text(encoding="utf-8"))["items"][0]


def test_growth_callback_kind_is_exact_and_bounded():
    kind = growth_callback_kind("2026-05-27-clear_bridge")
    assert kind == "pulse.conversation.growth:2026-05-27-clear_bridge"
    assert growth_id_from_callback_kind(kind) == "2026-05-27-clear_bridge"
    assert growth_id_from_callback_kind("pulse.conversation.growth:bad/id") is None


def test_http_acceptance_is_pending_not_offered(tmp_path):
    path = tmp_path / "growth-material.json"
    _write(path, [{"id": "g1", "status": "candidate"}])

    result = mark_growth_dispatch_pending(
        path,
        "g1",
        now=datetime(2026, 8, 30, 12, 0).astimezone(),
    )

    item = _item(path)
    assert result["changed"] is True
    assert item["status"] == "candidate"
    assert item["dispatch_pending_until"]
    assert "offered_at" not in item


def test_terminal_visible_output_moves_exact_item_to_awaiting_lisa_once(tmp_path):
    path = tmp_path / "growth-material.json"
    _write(path, [{"id": "g1", "status": "candidate"}])
    mark_growth_dispatch_pending(path, "g1")

    first = mark_growth_terminal_result(
        path,
        "g1",
        status="ok",
        run_id="run-1",
        output_text="Я заметил одну грань. Как ты её понимаешь?",
    )
    second = mark_growth_terminal_result(
        path,
        "g1",
        status="ok",
        run_id="run-1",
        output_text="Я заметил одну грань. Как ты её понимаешь?",
    )

    item = _item(path)
    assert first["changed"] is True
    assert second == {
        "changed": False,
        "reason": "already-awaiting-lisa",
        "id": "g1",
        "status": "awaiting_lisa",
    }
    assert item["status"] == "awaiting_lisa"
    assert item["offered_run_id"] == "run-1"
    assert len(item["visible_output_sha256"]) == 64
    assert "dispatch_pending_until" not in item


def test_terminal_failure_does_not_claim_lisa_saw_item(tmp_path):
    path = tmp_path / "growth-material.json"
    _write(path, [{"id": "g1", "status": "candidate"}])
    mark_growth_dispatch_pending(path, "g1")

    result = mark_growth_terminal_result(
        path,
        "g1",
        status="error",
        run_id="run-failed",
    )

    item = _item(path)
    assert result["delivery"] == "failed"
    assert item["status"] == "candidate"
    assert item["delivery_retry_after"]
    assert "offered_at" not in item
