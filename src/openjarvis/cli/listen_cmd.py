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

_LATEX_INLINE = _re.compile(r"\$\$?(.+?)\$\$?")
_LATEX_FRAC = _re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
_MD_BOLD_ITALIC = _re.compile(r"[*_]{1,3}")
_MD_HEADER = _re.compile(r"^#{1,6}\s*", flags=_re.MULTILINE)
_BACKSLASH_CMD = _re.compile(r"\\[a-zA-Z]+")


def _speech_clean(text: str) -> str:
    """Strip LaTeX/markdown artifacts that TTS would otherwise read literally."""
    text = _LATEX_FRAC.sub(r"(\1) over (\2)", text)
    text = _LATEX_INLINE.sub(r"\1", text)
    text = _BACKSLASH_CMD.sub("", text)
    text = _MD_HEADER.sub("", text)
    text = _MD_BOLD_ITALIC.sub("", text)
    text = text.replace("$", "").replace("\\", "").replace("`", "")
    text = _re.sub(r"[ \t]+", " ", text)
    return text.strip()


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
                result = system.ask(text, voice=True)
                console.print(f"[dim]timing: agent {time.perf_counter() - t_ask:.1f}s[/dim]")
                content = _speech_clean(result.get("content", ""))
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
