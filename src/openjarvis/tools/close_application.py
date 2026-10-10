"""Local application closer -- asks a running app's window to close.

Finds windows directly through the Win32 API and sends WM_CLOSE (the same
request as clicking the X, so apps with unsaved work can still prompt),
then checks the window actually went away before reporting success. It
never kills a process, so shared hosts such as ApplicationFrameHost (which
backs every Store app) are not touched.

Security model: zero required capabilities; the boundary is matching only
genuinely open windows by app name, never an arbitrary target.
"""

from __future__ import annotations

import time
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec
from openjarvis.tools.switch_to_application import _Win32

_WM_CLOSE = 0x0010
# Never close the window Jarvis itself is running in.
_PROTECTED_EXES = {
    "windowsterminal", "powershell", "pwsh", "cmd", "conhost",
    "openconsole", "python", "pythonw",
}


def _app_label(title: str) -> str:
    """The app-name part of a window title: 'ask.py - Notepad' -> 'Notepad'.
    Document and tab names are ignored, so 'close news' can't close a
    browser just because one tab is called 'News'."""
    for sep in (" - ", " \u2013 ", " \u2014 "):
        if sep in title:
            title = title.rsplit(sep, 1)[-1]
    return title


def _close_window(api: _Win32, hwnd: int) -> str:
    """Returns 'closed', 'open' (still there after 4 s) or 'blocked'."""
    wt = api.wt
    user32 = api.user32
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostMessageW.restype = wt.BOOL
    user32.IsWindow.argtypes = [wt.HWND]
    if not user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0):
        return "blocked"
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return "closed"
        time.sleep(0.25)
    return "open"


@ToolRegistry.register("close_application")
class CloseApplicationTool(BaseTool):
    """Close a running application by asking its window to close."""

    tool_id = "close_application"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="close_application",
            description=(
                "Close a running application by name (e.g. 'spotify', "
                "'microsoft store', 'edge'). Matches against windows that are "
                "actually open right now -- if the name is ambiguous or "
                "nothing matching is currently running, say so plainly "
                "rather than guessing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Name of the running application to close.",
                    }
                },
                "required": ["app_name"],
            },
            category="system",
            required_capabilities=[],
        )

    def execute(self, **params: Any) -> ToolResult:
        app_name = str(params.get("app_name", "")).strip()
        if not app_name:
            return ToolResult(tool_name="close_application", content="No app_name provided.", success=False)

        try:
            api = _Win32()
            windows = api.list_windows()
        except Exception as exc:
            return ToolResult(
                tool_name="close_application",
                content=f"Could not list open windows: {exc}",
                success=False,
            )

        query = app_name.lower()
        candidates = [
            w for w in windows
            if query in w["exe"] or query in _app_label(w["title"]).lower()
        ]
        if not candidates:
            return ToolResult(
                tool_name="close_application",
                content=f"Nothing matching '{app_name}' is currently open.",
                success=False,
            )

        # Store apps share one host exe, so tell them apart by title; every
        # other app by executable. Several windows of one app are fine (we
        # close the topmost); different apps are ambiguous.
        def key(w: dict[str, Any]) -> str:
            return w["title"] if w["exe"] == "applicationframehost" else w["exe"]

        if len({key(w) for w in candidates}) > 1:
            options = ", ".join(sorted({_app_label(w["title"]) or w["exe"] for w in candidates}))
            return ToolResult(
                tool_name="close_application",
                content=f"'{app_name}' matches different open apps: {options}. Please be more specific.",
                success=False,
            )

        target = candidates[0]
        if target["exe"] in _PROTECTED_EXES:
            return ToolResult(
                tool_name="close_application",
                content="I won't close the terminal Jarvis is running in.",
                success=False,
            )

        label = _app_label(target["title"]) or target["exe"]
        same = [w for w in candidates if key(w) == key(target)]
        try:
            status = _close_window(api, target["hwnd"])
        except Exception as exc:
            return ToolResult(
                tool_name="close_application",
                content=f"Found {label} but failed to close it: {exc}",
                success=False,
            )
        if status == "blocked":
            return ToolResult(
                tool_name="close_application",
                content=f"Windows would not let me close {label}.",
                success=False,
            )
        if status == "open":
            return ToolResult(
                tool_name="close_application",
                content=f"Asked {label} to close, but it is still open -- it may be waiting for you to save.",
                success=False,
            )
        more = len(same) - 1
        extra = f" {more} more {label} window{'s' if more != 1 else ''} still open." if more else ""
        return ToolResult(
            tool_name="close_application",
            content=f"Closed {label}.{extra}",
            success=True,
        )