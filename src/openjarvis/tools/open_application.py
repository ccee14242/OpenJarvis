"""Local application launcher -- opens a small, fixed allowlist of apps.

Security model: zero required capabilities (like calculator), because the
real boundary here is the hardcoded ALLOWED_APPS dict, not the policy layer.
Jarvis can never launch anything outside this list, regardless of phrasing,
since the tool only ever looks up a name in this dict -- it never accepts
or constructs an arbitrary path or command from model output.
"""

from __future__ import annotations

import subprocess
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

# name -> ("exe", path) for a direct executable, or ("uwp", AppID) for a
# Microsoft Store / UWP packaged app launched via explorer.exe shell:AppsFolder.
ALLOWED_APPS: dict[str, tuple[str, str]] = {
    "vs code": ("exe", r"C:\Users\A\AppData\Local\Programs\Microsoft VS Code\Code.exe"),
    "visual studio code": ("exe", r"C:\Users\A\AppData\Local\Programs\Microsoft VS Code\Code.exe"),
    "spotify": ("uwp", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"),
    "file explorer": ("exe", "explorer.exe"),
    "explorer": ("exe", "explorer.exe"),
    "edge": ("uwp", "MSEdge"),
    "browser": ("uwp", "MSEdge"),
    "microsoft edge": ("uwp", "MSEdge"),
}


@ToolRegistry.register("open_application")
class OpenApplicationTool(BaseTool):
    """Open one of a small, fixed set of allowed desktop applications."""

    tool_id = "open_application"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        names = ", ".join(sorted({n for n in ALLOWED_APPS if " " not in n or n == "vs code"}))
        return ToolSpec(
            name="open_application",
            description=(
                "Open a desktop application by name. Only a fixed, pre-approved "
                f"list of apps can be opened: {names}. If the requested app is "
                "not in this list, say so plainly rather than attempting "
                "anything else -- never try another tool or method to open it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Name of the application to open, e.g. 'vs code' or 'spotify'.",
                    }
                },
                "required": ["app_name"],
            },
            category="system",
            required_capabilities=[],
        )

    def execute(self, **params: Any) -> ToolResult:
        app_name = str(params.get("app_name", "")).strip().lower()
        if app_name not in ALLOWED_APPS:
            allowed = ", ".join(sorted(set(ALLOWED_APPS.keys())))
            return ToolResult(
                tool_name="open_application",
                content=f"'{app_name}' is not in the allowed application list. Allowed: {allowed}",
                success=False,
            )

        kind, target = ALLOWED_APPS[app_name]
        try:
            if kind == "uwp":
                subprocess.Popen(
                    ["explorer.exe", f"shell:AppsFolder\\{target}"],
                )
            else:
                subprocess.Popen([target])
            return ToolResult(
                tool_name="open_application",
                content=f"Opened {app_name}.",
                success=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="open_application",
                content=f"Failed to open {app_name}: {exc}",
                success=False,
            )