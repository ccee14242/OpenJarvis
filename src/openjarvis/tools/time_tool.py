"""Local clock tool -- answers date/time questions with zero cost."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("get_current_time")
class GetCurrentTimeTool(BaseTool):
    """Return the current local date and time. No network, no model call."""

    tool_id = "get_current_time"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_current_time",
            description=(
                "Get the current local date and time. Use this whenever asked "
                "the time, date, or day of the week -- never guess or say you "
                "don't have access to a clock."
            ),
            parameters={"type": "object", "properties": {}},
            category="knowledge",
            required_capabilities=[],
        )

    def execute(self, **params: Any) -> ToolResult:
        now = datetime.now().astimezone()
        return ToolResult(
            tool_name="get_current_time",
            content=now.strftime("%A, %B %d, %Y, %I:%M %p %Z"),
            success=True,
        )