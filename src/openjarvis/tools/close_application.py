"""Local application closer -- closes a running app by matching its visible
window title or process name against what's actually running.

Security model: same as open_application -- zero required capabilities,
matched only against genuinely running processes with a visible window.
Never accepts or constructs an arbitrary process name/PID from model output
without first confirming a real match exists.
"""

from __future__ import annotations

import subprocess
from difflib import get_close_matches
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


def _list_running_windows() -> list[dict[str, str]]:
    """Return [{"ProcessName": ..., "Title": ..., "Id": ...}, ...] for every
    process with a visible main window. Raises on failure -- callers handle."""
    import json

    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | "
            "Select-Object ProcessName, MainWindowTitle, Id | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    data = json.loads(result.stdout)
    if isinstance(data, dict):
        data = [data]
    return [
        {"ProcessName": d.get("ProcessName", ""), "Title": d.get("MainWindowTitle", ""), "Id": str(d.get("Id", ""))}
        for d in data
    ]


@ToolRegistry.register("close_application")
class CloseApplicationTool(BaseTool):
    """Close a running application by matching its window title or process
    name against what's genuinely running -- never an invented target."""

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
            windows = _list_running_windows()
        except Exception as exc:
            return ToolResult(
                tool_name="close_application",
                content=f"Could not list running windows: {exc}",
                success=False,
            )

        query = app_name.lower()
        candidates = [
            w for w in windows
            if query in w["Title"].lower() or query in w["ProcessName"].lower()
        ]

        if len(candidates) == 0:
            names = ", ".join(sorted({w["ProcessName"] for w in windows}))
            close = get_close_matches(app_name, [w["Title"] for w in windows] + [w["ProcessName"] for w in windows], n=3, cutoff=0.6)
            suggestion = f" Did you mean: {', '.join(close)}?" if close else f" Currently open: {names}."
            return ToolResult(
                tool_name="close_application",
                content=f"Nothing matching '{app_name}' is currently running.{suggestion}",
                success=False,
            )
        if len(candidates) > 1:
            options = ", ".join(sorted({w["Title"] or w["ProcessName"] for w in candidates}))
            return ToolResult(
                tool_name="close_application",
                content=f"'{app_name}' matches multiple open windows: {options}. Please be more specific.",
                success=False,
            )

        target = candidates[0]
        try:
            subprocess.run(["taskkill", "/PID", target["Id"]], capture_output=True, timeout=10, check=True)
            return ToolResult(
                tool_name="close_application",
                content=f"Closed {target['Title'] or target['ProcessName']}.",
                success=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="close_application",
                content=f"Found {target['Title'] or target['ProcessName']} but failed to close it: {exc}",
                success=False,
            )