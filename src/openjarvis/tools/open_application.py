"""Local application launcher -- dynamically matches against Windows'
own installed-app list (Get-StartApps), never a model-invented path.

Security model: the model can never specify an arbitrary executable path
or command. It can only supply a search name, which is matched against
what Get-StartApps reports is genuinely installed on this machine. If no
confident single match exists, the tool refuses rather than guessing --
it never launches an ambiguous or low-confidence match.
"""

from __future__ import annotations

import json
import subprocess
from difflib import get_close_matches
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


def _list_installed_apps() -> list[dict[str, str]]:
    """Return [{"Name": ..., "AppID": ...}, ...] from Windows' own registry
    of installed/start-menu apps. Raises on failure -- callers must handle."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    data = json.loads(result.stdout)
    if isinstance(data, dict):
        data = [data]
    return data


def _launch(app: dict[str, str]) -> None:
    app_id = app["AppID"]
    if "!" in app_id:
        # UWP/Store package AppID -> launch via explorer's AppsFolder virtual path.
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
    else:
        # Legacy app: AppID is a real path (.exe or .lnk).
        subprocess.Popen(["explorer.exe", app_id]) if app_id.lower().endswith(".lnk") else subprocess.Popen([app_id])


@ToolRegistry.register("open_application")
class OpenApplicationTool(BaseTool):
    """Open an installed application by name, matched against the live
    Windows app list -- never an arbitrary or model-invented path."""

    tool_id = "open_application"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="open_application",
            description=(
                "Open an installed desktop application by name (e.g. 'spotify', "
                "'firefox', 'notepad'). Matches against the applications actually "
                "installed on this machine -- if the name is ambiguous or not "
                "found, say so plainly rather than guessing or trying another method."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Name of the application to open, e.g. 'spotify' or 'firefox'.",
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
            return ToolResult(tool_name="open_application", content="No app_name provided.", success=False)

        try:
            apps = _list_installed_apps()
        except Exception as exc:
            return ToolResult(
                tool_name="open_application",
                content=f"Could not list installed applications: {exc}",
                success=False,
            )

        names = [a["Name"] for a in apps]
        query = app_name.lower()

        # Exact (case-insensitive) match first.
        exact = [a for a in apps if a["Name"].lower() == query]
        if len(exact) == 1:
            match = exact[0]
        else:
            # Substring match: query appears in the app name, or vice versa.
            substring = [a for a in apps if query in a["Name"].lower() or a["Name"].lower() in query]
            if len(substring) == 1:
                match = substring[0]
            elif len(substring) > 1:
                options = ", ".join(sorted({a["Name"] for a in substring}))
                return ToolResult(
                    tool_name="open_application",
                    content=f"'{app_name}' matches multiple installed apps: {options}. Please be more specific.",
                    success=False,
                )
            else:
                close = get_close_matches(app_name, names, n=3, cutoff=0.6)
                if len(close) == 1:
                    match = next(a for a in apps if a["Name"] == close[0])
                else:
                    suggestion = f" Did you mean: {', '.join(close)}?" if close else ""
                    return ToolResult(
                        tool_name="open_application",
                        content=f"'{app_name}' is not an installed application.{suggestion}",
                        success=False,
                    )

        try:
            _launch(match)
            return ToolResult(
                tool_name="open_application",
                content=f"Opened {match['Name']}.",
                success=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="open_application",
                content=f"Found {match['Name']} but failed to launch it: {exc}",
                success=False,
            )