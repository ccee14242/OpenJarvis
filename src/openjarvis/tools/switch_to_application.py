"""Local application focus-switcher -- brings an already-running window to
the foreground. Finds windows directly via the Win32 API (not by process
list), restores minimized windows, and reports success only if Windows
actually made the window the foreground window.

Security model: zero required capabilities; the real boundary is matching
only against genuinely open windows, never an arbitrary target.
"""

from __future__ import annotations

import time
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SW_RESTORE = 9
_DWMWA_CLOAKED = 14
# The invisible UWP content window and desktop/taskbar plumbing are never
# what a user means by "switch to X"; the visible frame window is.
_SKIP_CLASSES = {"Windows.UI.Core.CoreWindow", "Progman", "WorkerW", "Shell_TrayWnd"}


class _Win32:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ct = ctypes
        self.wt = wintypes
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        k = self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            self.dwm = ctypes.WinDLL("dwmapi")
        except OSError:
            self.dwm = None

        self.EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [self.EnumProc, wintypes.LPARAM]
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]

    def _exe_name(self, hwnd) -> str:
        pid = self.wt.DWORD(0)
        self.user32.GetWindowThreadProcessId(hwnd, self.ct.byref(pid))
        handle = self.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            size = self.wt.DWORD(1024)
            buf = self.ct.create_unicode_buffer(1024)
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, self.ct.byref(size)):
                return ""
            name = buf.value.replace("/", "\\").split("\\")[-1].lower()
            return name[:-4] if name.endswith(".exe") else name
        finally:
            self.kernel32.CloseHandle(handle)

    def _cloaked(self, hwnd) -> bool:
        if self.dwm is None:
            return False
        try:
            val = self.ct.c_int(0)
            res = self.dwm.DwmGetWindowAttribute(
                self.wt.HWND(hwnd), _DWMWA_CLOAKED, self.ct.byref(val), self.ct.sizeof(val)
            )
            return res == 0 and val.value != 0
        except Exception:
            return False

    def list_windows(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        def cb(hwnd, lparam):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            n = self.user32.GetWindowTextLengthW(hwnd)
            if not n:
                return True
            tbuf = self.ct.create_unicode_buffer(n + 1)
            self.user32.GetWindowTextW(hwnd, tbuf, n + 1)
            cbuf = self.ct.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd, cbuf, 256)
            if cbuf.value in _SKIP_CLASSES or self._cloaked(hwnd):
                return True
            found.append({
                "hwnd": hwnd,
                "title": tbuf.value,
                "cls": cbuf.value,
                "exe": self._exe_name(hwnd),
                "iconic": bool(self.user32.IsIconic(hwnd)),
            })
            return True

        self.user32.EnumWindows(self.EnumProc(cb), 0)
        return found  # EnumWindows order is top-to-bottom z-order

    def bring_to_front(self, win: dict[str, Any]) -> bool:
        hwnd = win["hwnd"]
        if win["iconic"]:
            self.user32.ShowWindow(hwnd, _SW_RESTORE)
            time.sleep(0.4)
        # A synthetic Alt press is the usual way past Windows' foreground lock.
        self.user32.keybd_event(0x12, 0, 0, 0)
        self.user32.keybd_event(0x12, 0, 2, 0)
        self.user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        return self.user32.GetForegroundWindow() == hwnd


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
                "Bring an already-open application window to the front "
                "(switch to, go to, or focus on an app). Use THIS tool, not "
                "the calculator tool, when the user says things like "
                "'switch to calculator' or names any app to switch to: the "
                "calculator tool only does arithmetic and cannot switch "
                "windows. Only works for apps that are already open -- if "
                "nothing matching is running, say so plainly rather than "
                "opening a new instance or guessing."
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
            api = _Win32()
            windows = api.list_windows()
        except Exception as exc:
            return ToolResult(
                tool_name="switch_to_application",
                content=f"Could not list open windows: {exc}",
                success=False,
            )

        query = app_name.lower()
        candidates = [w for w in windows if query in w["title"].lower() or query in w["exe"]]

        if not candidates:
            return ToolResult(
                tool_name="switch_to_application",
                content=f"Nothing matching '{app_name}' is currently open.",
                success=False,
            )

        # Store apps all share one host exe, so tell them apart by title;
        # everything else by executable. Several windows of one app are fine
        # (we take the topmost); different apps are ambiguous.
        def key(w: dict[str, Any]) -> str:
            return w["title"] if w["exe"] == "applicationframehost" else w["exe"]

        if len({key(w) for w in candidates}) > 1:
            options = ", ".join(sorted({w["title"] for w in candidates}))
            return ToolResult(
                tool_name="switch_to_application",
                content=f"'{app_name}' matches different open apps: {options}. Please be more specific.",
                success=False,
            )

        target = candidates[0]
        try:
            ok = api.bring_to_front(target)
        except Exception as exc:
            return ToolResult(
                tool_name="switch_to_application",
                content=f"Found {target['title']} but failed to switch to it: {exc}",
                success=False,
            )
        if not ok:
            return ToolResult(
                tool_name="switch_to_application",
                content=f"Found {target['title']} but Windows would not bring it to the front.",
                success=False,
            )
        return ToolResult(
            tool_name="switch_to_application",
            content=f"Switched to {target['title']}.",
            success=True,
        )