"""Reminders and timers, stored in a small JSON file.

Nothing here runs on its own: `jarvis listen` reads the file between wake-word
listens and speaks whatever is due. A reminder is plain text -- it never runs a
command or an agent task -- so the tool needs no capabilities. Its boundary is
the limits below (24 hours, 50 pending, 200 characters).
"""

from __future__ import annotations

import contextlib
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
_router_active = False


@contextlib.contextmanager
def router_call():
    """Marks calls made by the voice command router. Only those may set a
    reminder: the 4B model called set_reminder on garbled speech and invented
    durations, so model-initiated sets are refused."""
    global _router_active
    _router_active = True
    try:
        yield
    finally:
        _router_active = False
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


def _describe(r: dict) -> str:
    when = time.strftime("%I:%M %p", time.localtime(r["due"])).lstrip("0")
    if r.get("kind") == "timer":
        return f"a timer at {when}"
    return f"{r['message']} at {when}"


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
                "List or cancel pending reminders and timers (action 'list' or "
                "'cancel'). Do NOT use this tool to set one: setting is handled "
                "by the voice command router. If the user wants a reminder, tell "
                "them to say, for example, 'remind me in ten minutes to check "
                "the oven'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "seconds": {"type": "number", "description": "Seconds from now until it goes off."},
                    "message": {"type": "string", "description": "What to say when it goes off."},
                    "kind": {"type": "string", "description": "'reminder' (default) or 'timer'. With action 'cancel', limits what is cancelled."},
                    "action": {"type": "string", "description": "'set' (default), 'list' to read back pending reminders, or 'cancel'."},
                },
                "required": [],
            },
            category="system",
            required_capabilities=[],
        )

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action", "set") or "set").lower()
        if action == "list":
            items = sorted(load_reminders(), key=lambda r: r["due"])
            if not items:
                return ToolResult(tool_name="set_reminder", content="No reminders are set.", success=True)
            shown = "; ".join(_describe(r) for r in items[:5])
            extra = f"; and {len(items) - 5} more" if len(items) > 5 else ""
            noun = "reminder" if len(items) == 1 else "reminders"
            return ToolResult(
                tool_name="set_reminder",
                content=f"You have {len(items)} {noun}: {shown}{extra}.",
                success=True,
            )
        if action == "cancel":
            kind = str(params.get("kind", "")).lower()
            items = load_reminders()
            if kind in ("reminder", "timer"):
                keep = [r for r in items if r.get("kind", "reminder") != kind]
            else:
                keep = []
            try:
                _save(keep)
            except Exception as exc:
                return _fail(f"Could not cancel: {exc}")
            n = len(items) - len(keep)
            if n == 0:
                return ToolResult(tool_name="set_reminder", content="There was nothing to cancel.", success=True)
            noun = "reminder" if n == 1 else "reminders"
            return ToolResult(tool_name="set_reminder", content=f"Cancelled {n} {noun}.", success=True)
        if action != "set":
            return _fail("action must be set, list or cancel.")
        if not _router_active:
            return _fail(
                "I can only set reminders from a clear voice command, for example: "
                "remind me in ten minutes to check the oven."
            )
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