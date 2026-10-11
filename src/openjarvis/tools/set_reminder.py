"""Reminders and timers, stored in a small JSON file.

Nothing here runs on its own: `jarvis listen` reads the file between wake-word
listens and speaks whatever is due. A reminder is plain text -- it never runs a
command or an agent task -- so the tool needs no capabilities. Its boundary is
the limits below (24 hours, 50 pending, 200 characters).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

MAX_SECONDS = 24 * 3600
_MAX_PENDING = 50
_MAX_MESSAGE = 200


def _path() -> Path:
    return Path.home() / ".openjarvis" / "reminders.json"


def load_reminders() -> list:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [
        r for r in data
        if isinstance(r, dict)
        and isinstance(r.get("id"), str)
        and isinstance(r.get("due"), (int, float))
        and isinstance(r.get("message"), str)
    ]


def _save(items: list) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(items), encoding="utf-8")
    os.replace(tmp, p)


def due_reminders(now: Optional[float] = None) -> list:
    now = time.time() if now is None else now
    return sorted((r for r in load_reminders() if r["due"] <= now), key=lambda r: r["due"])


def remove_reminders(ids: list) -> None:
    if not ids:
        return
    gone = set(ids)
    _save([r for r in load_reminders() if r["id"] not in gone])


def seconds_until_next(now: Optional[float] = None) -> Optional[float]:
    items = load_reminders()
    if not items:
        return None
    now = time.time() if now is None else now
    return max(0.0, min(r["due"] for r in items) - now)


def _fail(message: str) -> ToolResult:
    return ToolResult(tool_name="set_reminder", content=message, success=False)


@ToolRegistry.register("set_reminder")
class SetReminderTool(BaseTool):
    """Set a spoken reminder or timer that goes off after some seconds."""

    tool_id = "set_reminder"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="set_reminder",
            description=(
                "Set a spoken reminder or timer that goes off after a number of "
                "seconds (at most 24 hours). Convert the user's duration to "
                "seconds yourself. It is only spoken while the voice assistant "
                "is running."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "seconds": {"type": "number", "description": "Seconds from now until it goes off."},
                    "message": {"type": "string", "description": "What to say when it goes off."},
                    "kind": {"type": "string", "description": "'reminder' (default) or 'timer'."},
                },
                "required": ["seconds", "message"],
            },
            category="system",
            required_capabilities=[],
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            seconds = float(params.get("seconds"))
        except (TypeError, ValueError):
            return _fail("seconds must be a number.")
        message = str(params.get("message", "")).strip()[:_MAX_MESSAGE]
        kind = "timer" if str(params.get("kind", "")).lower() == "timer" else "reminder"
        if not message:
            return _fail("message is empty.")
        if not (1 <= seconds <= MAX_SECONDS):
            return _fail("seconds must be between 1 and 86400.")
        items = load_reminders()
        if len(items) >= _MAX_PENDING:
            return _fail("Too many pending reminders.")
        due = time.time() + seconds
        try:
            _save(items + [{"id": uuid.uuid4().hex, "due": due, "message": message, "kind": kind}])
        except Exception as exc:
            return _fail(f"Could not save the reminder: {exc}")
        when = time.strftime("%I:%M %p", time.localtime(due)).lstrip("0")
        return ToolResult(tool_name="set_reminder", content=f"Set for {when}: {message}", success=True)