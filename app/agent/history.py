from __future__ import annotations

import json
import logging
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

LOGGER = logging.getLogger(__name__)
_history_lock = Lock()
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _history_path(raw_path: str | Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def append_history_turn(
    raw_path: str | Path,
    *,
    conversation_id: str,
    request_id: str,
    question: str,
    provider: str,
    model: str,
    payload: dict[str, Any],
) -> None:
    """Best-effort local append for Ask history.

    This log is a convenience layer for a local-only UI. Disk failures should
    never change the API response path, so errors are logged at warning level.
    """

    if not raw_path:
        return
    path = _history_path(raw_path)
    record = {
        "version": 1,
        "created_at": _now_iso(),
        "conversation_id": conversation_id,
        "request_id": request_id,
        "question": question,
        "provider": provider,
        "model": model,
        "payload": payload,
    }
    try:
        with _history_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str, sort_keys=True))
                handle.write("\n")
    except OSError as exc:
        LOGGER.warning("Could not write Ask history log: %s", exc)


def read_history(raw_path: str | Path, *, limit: int = 25) -> dict[str, Any]:
    if not raw_path:
        return {"conversations": []}
    path = _history_path(raw_path)
    if not path.exists():
        return {"conversations": []}

    conversations: OrderedDict[str, dict[str, Any]] = OrderedDict()
    try:
        with _history_lock:
            lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        LOGGER.warning("Could not read Ask history log: %s", exc)
        return {"conversations": []}

    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        conversation_id = str(record.get("conversation_id") or "").strip()
        if not conversation_id:
            continue
        payload = (
            record.get("payload") if isinstance(record.get("payload"), dict) else {}
        )
        question = str(record.get("question") or "").strip()
        turn = {
            "created_at": record.get("created_at") or "",
            "request_id": record.get("request_id") or "",
            "question": question,
            "provider": record.get("provider") or "",
            "model": record.get("model") or "",
            "payload": payload,
        }
        existing = conversations.pop(conversation_id, None) or {
            "conversation_id": conversation_id,
            "title": question or "Ask NBA Stats chat",
            "created_at": turn["created_at"],
            "updated_at": turn["created_at"],
            "turns": [],
        }
        if question and not existing.get("title"):
            existing["title"] = question
        existing["updated_at"] = turn["created_at"] or existing.get("updated_at") or ""
        existing["turns"].append(turn)
        existing["turns"] = existing["turns"][-20:]
        conversations[conversation_id] = existing

    items = list(reversed(list(conversations.values())))[: max(1, limit)]
    return {"conversations": items}


def clear_history(raw_path: str | Path) -> None:
    if not raw_path:
        return
    path = _history_path(raw_path)
    try:
        with _history_lock:
            if path.exists():
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.warning("Could not clear Ask history log: %s", exc)
