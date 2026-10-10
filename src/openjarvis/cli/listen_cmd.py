"""jarvis listen -- always-on wake-word voice assistant.

Unlike jarvis chat --voice (push-to-talk: press Enter, then speak),
this command runs continuously: it listens for the wake word in the
background, and on each detection records one command, answers it, and
returns to listening -- no further input needed between exchanges.
"""

from __future__ import annotations

import time
from typing import Optional

import click
from rich.console import Console

from openjarvis.cli._voice_chat import VOICE_EXIT, VoiceSession, record_voice, speak

import re as _re

try:
    from num2words import num2words as _num2words_raw

    def _num2words(n, lang="en"):
        """num2words wrapper: strips the library\'s own "nine hundred and one"
        -style "and", which it always inserts and offers no flag to disable.
        Only touches the isolated number-word string we generate ourselves,
        never the surrounding sentence, so a real "and" written by the model
        elsewhere is never affected."""
        return _num2words_raw(n, lang=lang).replace(" and ", " ")
except ImportError:
    _num2words = None

_LATEX_INLINE = _re.compile(r"\$\$?(.+?)\$\$?")
_LATEX_FRAC = _re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
_MD_BOLD_ITALIC = _re.compile(r"[*_]{1,3}")
_MD_HEADER = _re.compile(r"^#{1,6}\s*", flags=_re.MULTILINE)
_BACKSLASH_CMD = _re.compile(r"\\[a-zA-Z]+")

# Currency first (so the $ is consumed before generic number handling sees
# the digits), then plain comma-grouped or plain integers/decimals.
_CURRENCY = _re.compile(r"\$\s?(\d[\d,]*)(?:\.(\d{1,2}))?")
_PLAIN_NUMBER = _re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d+))?(?![\w])"
)


def _normalize_numbers(text: str) -> str:
    """Convert digit sequences to spoken words via num2words.

    Runs before LaTeX/markdown stripping so currency symbols and comma
    grouping are still present to detect. One consistent spoken convention
    is used regardless of Kokoro/misaki\'s own internal guesswork.
    """
    if _num2words is None:
        return text

    def _currency_repl(m: _re.Match) -> str:
        dollars = int(m.group(1).replace(",", ""))
        cents = m.group(2)
        try:
            words = _num2words(dollars, lang="en_US")
        except Exception:
            return m.group(0)
        out = f"{words} dollar" + ("s" if dollars != 1 else "")
        if cents:
            cents_val = int(cents.ljust(2, "0"))
            try:
                cents_words = _num2words(cents_val, lang="en_US")
                out += f" and {cents_words} cent" + ("s" if cents_val != 1 else "")
            except Exception:
                pass
        return out

    def _plain_repl(m: _re.Match) -> str:
        whole = m.group(1).replace(",", "")
        frac = m.group(2)
        try:
            if frac:
                whole_words = _num2words(int(whole), lang="en_US")
                frac_words = " ".join(_num2words(int(d), lang="en_US") for d in frac)
                return f"{whole_words} point {frac_words}"
            return _num2words(int(whole), lang="en_US")
        except Exception:
            return m.group(0)

    text = _CURRENCY.sub(_currency_repl, text)
    text = _PLAIN_NUMBER.sub(_plain_repl, text)
    return text


def _speech_clean(text: str) -> str:
    """Normalize numbers, then strip LaTeX/markdown artifacts, for TTS."""
    text = _normalize_numbers(text)
    text = _LATEX_FRAC.sub(r"(\1) over (\2)", text)
    text = _LATEX_INLINE.sub(r"\1", text)
    text = _BACKSLASH_CMD.sub("", text)
    text = _MD_HEADER.sub("", text)
    text = _MD_BOLD_ITALIC.sub("", text)
    text = text.replace("$", "").replace("\\", "").replace("`", "")
    text = _re.sub(r"[ \t]+", " ", text)
    return text.strip()


_SENTENCE_BOUNDARY = _re.compile(r"(?<=[.!?])\s+")


_ROUTE_VERBS = _re.compile(
    r"^\s*(?:please\s+)?(open|launch|close|switch\s+to)\s+(?:the\s+|my\s+)?(.+?)[\s.!?,]*$",
    _re.IGNORECASE,
)
_ROUTE_TOOLS = {
    "open": ("open_application", "Opened"),
    "launch": ("open_application", "Opened"),
    "close": ("close_application", "Closed"),
    "switch to": ("switch_to_application", "Switched to"),
}
_PROTECTED_CLOSE = {
    "powershell", "windows powershell", "terminal", "windows terminal",
    "command prompt", "cmd", "jarvis",
}


def _try_route_command(system, text: str):
    """Deterministic fast path for 'open X', 'close X' and 'switch to X'.

    Returns the sentence to speak, or None to fall through to the model.
    Calls go through system.tool_executor (not tool.execute directly) so
    the capability policy, rate limiter and audit log still apply.
    """
    m = _ROUTE_VERBS.match(text)
    if not m:
        return None
    verb = " ".join(m.group(1).lower().split())
    app = m.group(2).strip()
    if not app or len(app.split()) > 3:
        return None
    executor = getattr(system, "tool_executor", None)
    if executor is None:
        return None
    tool_name, past = _ROUTE_TOOLS[verb]
    if tool_name == "close_application" and app.lower() in _PROTECTED_CLOSE:
        return "I won't close the terminal I am running in, sir."
    try:
        import json as _json
        from openjarvis.core.types import ToolCall

        res = executor.execute(
            ToolCall(id="voice_route", name=tool_name, arguments=_json.dumps({"app_name": app}))
        )
    except Exception:
        return None
    if str(res.content).startswith("Unknown tool"):
        return None
    if res.success:
        return f"{past} {app}, sir."
    return str(res.content)


def _truncate_for_voice(text: str, max_sentences: int = 2) -> str:
    """Hard cap on spoken reply length: a code-level backstop, since the
    voice-mode system prompt instruction asking for brevity is not always
    followed by the model. Splits on sentence-ending punctuation (decimals
    are already converted to \'point\' by _speech_clean\'s number pass, so
    no stray periods from numbers interfere with this split). Silently
    drops anything past the cap rather than appending a notice, matching
    the original brevity instruction\'s intent."""
    parts = _SENTENCE_BOUNDARY.split(text.strip())
    if len(parts) <= max_sentences:
        return text
    return " ".join(parts[:max_sentences]).strip()


@click.command()
@click.option(
    "--wake-model",
    default=None,
    help="Override the configured wake-word model (default: config.speech.wakeword_model).",
)
def listen(wake_model: Optional[str]) -> None:
    """Continuously listen for the wake word, then handle one voice command."""
    from openjarvis.core.config import load_config
    from openjarvis.speech._discovery import get_wakeword_backend
    from openjarvis.system import SystemBuilder

    console = Console()
    config = load_config()

    wakeword = get_wakeword_backend(config, model_override=wake_model)
    if wakeword is None:
        console.print(
            "[red]No wake-word backend available.[/red] "
            "Check that openwakeword is installed and a microphone is "
            "connected (see `jarvis doctor`)."
        )
        raise SystemExit(1)

    try:
        system = SystemBuilder().build()
    except Exception as exc:
        console.print(f"[red]Could not start JarvisSystem: {exc}[/red]")
        raise SystemExit(1)

    try:
        vision_system = SystemBuilder().agent("native_openhands").model("gemma3:4b").build()
    except Exception as exc:
        console.print(f"[yellow]Screen-check unavailable: {exc}[/yellow]")
        vision_system = None

    _SCREEN_KEYWORDS = ("screen", "on my display", "what do you see", "look at this")

    voice_session = VoiceSession(config)
    wake_phrase = (wake_model or config.speech.wakeword_model).replace("_", " ")

    console.print(f"[green]Listening for the wake word ({wake_phrase!r})...[/green]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]")

    try:
        while True:
            try:
                heard = wakeword.listen()
            except KeyboardInterrupt:
                break
            if not heard:
                continue

            console.print("\n[bold cyan]Wake word detected.[/bold cyan]")
            t_rec = time.perf_counter()
            text = record_voice(console, voice_session)
            console.print(f"[dim]timing: listen+transcribe {time.perf_counter() - t_rec:.1f}s[/dim]")
            if text is VOICE_EXIT:
                break
            if not text:
                console.print(f"[green]Listening for the wake word ({wake_phrase!r})...[/green]")
                continue

            try:
                t_ask = time.perf_counter()
                _routed = _try_route_command(system, text)
                is_screen_request = _routed is None and vision_system is not None and any(
                    kw in text.lower() for kw in _SCREEN_KEYWORDS
                )
                if _routed is not None:
                    result = {"content": _routed}
                elif is_screen_request:
                    try:
                        import base64 as _b64
                        from openjarvis.cli._screen import capture_screen_to_temp

                        _shot = capture_screen_to_temp()
                        with open(_shot, "rb") as _f:
                            _img_b64 = _b64.b64encode(_f.read()).decode("ascii")
                        result = vision_system.ask(text, voice=True, images=[_img_b64])
                        _vc = result.get("content", "")
                        if _vc and not _vc.lower().startswith(("roughly", "it looks like", "i'm not certain")):
                            # Strip a leading honorific/name so it reads naturally after the
                            # caveat instead of colliding with it (e.g. "...it looks like sir, the...").
                            _vc_stripped = _vc
                            for _lead in ("sir, ", "Sir, "):
                                if _vc_stripped.startswith(_lead):
                                    _vc_stripped = _vc_stripped[len(_lead):]
                                    break
                            result["content"] = (
                                "Based on a rough look, sir -- "
                                + _vc_stripped[0].lower() + _vc_stripped[1:]
                            )
                    except Exception as exc:
                        console.print(f"[yellow]Screen capture failed: {exc}[/yellow]")
                        result = {
                            "content": (
                                "I couldn't check the screen just now -- "
                                "something went wrong on my end. Please try again."
                            )
                        }
                else:
                    result = system.ask(text, voice=True)
                console.print(f"[dim]timing: agent {time.perf_counter() - t_ask:.1f}s[/dim]")
                content = _truncate_for_voice(_speech_clean(result.get("content", "")))
            except Exception as exc:
                console.print(f"[red]Error: {exc}[/red]")
                console.print(f"[green]Listening for the wake word ({wake_phrase!r})...[/green]")
                continue

            console.print(f"[bold]Jarvis:[/bold] {content}")
            t_speak = time.perf_counter()
            speak(content, console, voice_session)
            console.print(f"[dim]timing: speak {time.perf_counter() - t_speak:.1f}s for {len(content)} chars[/dim]")
            console.print(f"[green]Listening for the wake word ({wake_phrase!r})...[/green]")
    except KeyboardInterrupt:
        pass
    finally:
        wakeword.stop()
        console.print("\n[dim]Stopped listening.[/dim]")
        if hasattr(system, "close"):
            try:
                system.close()
            except Exception:
                pass


__all__ = ["listen"]
