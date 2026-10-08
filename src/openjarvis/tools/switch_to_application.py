"""Local application focus-switcher -- brings an already-running window to
the foreground by matching its title/process name, same safe pattern as
open_application and close_application.

Security model: zero required capabilities; the real boundary is matching
only against genuinely running windows, never an arbitrary target.
"""

from __future__ import annotations

import subprocess
from difflib import get_close_matches
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


def _list_running_windows() -> list[dict[str, str]]:
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


@ToolRegistry.register("switch_to_application")
class SwitchToApplicationTool(BaseTool):
    """Bring an already-running application's window to the foreground."""

    tool_id = "switch_to_application"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="switch_to_application",
            description=(
                "Switch focus to an already-running application by name "
                "(e.g. 'spotify', 'edge'). Only works for applications that "
                "are already open -- if nothing matching is currently "
                "running, say so plainly rather than opening a new instance "
                "or guessing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Name of the already-running application to switch to.",
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
            return ToolResult(tool_name="switch_to_application", content="No app_name provided.", success=False)

        try:
            windows = _list_running_windows()
        except Exception as exc:
            return ToolResult(
                tool_name="switch_to_application",
                content=f"Could not list running windows: {exc}",
                success=False,
            )

        query = app_name.lower()
        candidates = [
            w for w in windows
            if query in w["Title"].lower() or query in w["ProcessName"].lower()
        ]

        if len(candidates) == 0:
            close = get_close_matches(
                app_name, [w["Title"] for w in windows] + [w["ProcessName"] for w in windows], n=3, cutoff=0.6
            )
            suggestion = f" Did you mean: {', '.join(close)}?" if close else ""
            return ToolResult(
                tool_name="switch_to_application",
                content=f"Nothing matching '{app_name}' is currently running.{suggestion}",
                success=False,
            )
        if len(candidates) > 1:
            options = ", ".join(sorted({w["Title"] or w["ProcessName"] for w in candidates}))
            return ToolResult(
                tool_name="switch_to_application",
                content=f"'{app_name}' matches multiple open windows: {options}. Please be more specific.",
                success=False,
            )

        target = candidates[0]
        try:
            subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    f"(New-Object -ComObject WScript.Shell).AppActivate({target['Id']})",
                ],
                capture_output=True,
                timeout=10,
                check=True,
            )
            return ToolResult(
                tool_name="switch_to_application",
                content=f"Switched to {target['Title'] or target['ProcessName']}.",
                success=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="switch_to_application",
                content=f"Found {target['Title'] or target['ProcessName']} but failed to switch to it: {exc}",
                success=False,
            )