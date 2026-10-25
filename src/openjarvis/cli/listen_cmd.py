"""jarvis listen -- always-on wake-word voice assistant.

Unlike jarvis chat --voice (push-to-talk: press Enter, then speak),
this command runs continuously: it listens for the wake word in the
background, and on each detection records one command, answers it, and
returns to listening -- no further input needed between exchanges.
"""

from __future__ import annotations

import queue
import threading
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

    def _time_repl(m: _re.Match) -> str:
        h, mi, mer = int(m.group(1)), int(m.group(2)), m.group(3)
        if h > 23 or mi > 59:
            return m.group(0)
        try:
            hw = _num2words(h, lang="en_US")
            mw = _num2words(mi, lang="en_US")
        except Exception:
            return m.group(0)
        if mi == 0:
            out = hw if mer else hw + " o'clock"
        elif mi < 10:
            out = hw + " oh " + mw
        else:
            out = hw + " " + mw
        if mer:
            out += " A M" if mer.lower() == "a" else " P M"
        return out

    text = _re.sub(
        r"(?<![\w:.])(\d{1,2}):(\d{2})(?:\s?([AaPp])\.?[Mm])?(?![\w:])", _time_repl, text
    )
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
    r"^\s*(?:(?:and|then|now|ok|okay|so|alright)[\s,]+)*(?:(?:can|could|would|will)\s+you\s+|you\s+)?(?:please[\s,]+)?(open|launch|close|switch\s+to)[\s,.:;-]+(?:the\s+|my\s+)?(.+?)[\s.!?,]*$",
    _re.IGNORECASE,
)
_ROUTE_TOOLS = {
    "open": ("open_application", "Opened"),
    "launch": ("open_application", "Opened"),
    "close": ("close_application", "Closed"),
    "switch to": ("switch_to_application", "Switched to"),
}
_ROUTE_IGNORE = {
    "it", "this", "that", "them", "those", "these", "window", "windows",
    "app", "apps", "up", "down", "all", "everything", "one", "now",
}
_PROTECTED_CLOSE = {
    "powershell", "windows powershell", "terminal", "windows terminal",
    "command prompt", "cmd", "jarvis",
}


_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
_UNIT_CANON = {
    "second": "second", "seconds": "second", "sec": "second", "secs": "second",
    "minute": "minute", "minutes": "minute", "min": "minute", "mins": "minute",
    "hour": "hour", "hours": "hour", "hr": "hour", "hrs": "hour",
}
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}
_LEAD = r"^\s*(?:(?:and|then|now|ok|okay|so|alright)[\s,]+)*(?:(?:can|could|would|will)\s+you\s+|you\s+)?(?:please[\s,]+)?"
_REMIND_IN = _re.compile(_LEAD + r"remind\s+me\s+in\s+(?P<dur>.+?)\s+(?:to|that)\s+(?P<msg>.+?)[\s.!?,]*$", _re.IGNORECASE)
_REMIND_TO_IN = _re.compile(_LEAD + r"remind\s+me\s+(?:to|that)\s+(?P<msg>.+)\s+in\s+(?P<dur>.+?)[\s.!?,]*$", _re.IGNORECASE)
_REMIND_BARE = _re.compile(_LEAD + r"remind\s+me\s+in\s+(?P<dur>.+?)[\s.!?,]*$", _re.IGNORECASE)
_REMIND_ANY = _re.compile(_LEAD + r"remind\s+me\b", _re.IGNORECASE)
_TIMER = _re.compile(_LEAD + r"(?:set|start|create)\s+(?:me\s+)?(?:an?\s+)?(?:(?P<a>.+?)\s+)?timer(?:\s+for\s+(?P<b>.+?))?[\s.!?,]*$", _re.IGNORECASE)


def _parse_count(s: str):
    s = s.strip()
    if _re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    if s in ("a", "an"):
        return 1.0
    words = s.split()
    if not words or any(w not in _NUM_WORDS for w in words):
        return None
    return float(sum(_NUM_WORDS[w] for w in words))


def _parse_duration(s: str):
    # Returns (seconds, spoken_label) or None.
    s = _re.sub(r"\s+", " ", _re.sub(r"[-,]", " ", s.lower())).strip()
    if s in ("half an hour", "half hour", "a half hour", "a half an hour"):
        return 1800.0, "half an hour"
    m = _re.fullmatch(r"(?P<n>\d+(?:\.\d+)?|[a-z]+(?: [a-z]+)?) ?(?P<u>seconds?|secs?|minutes?|mins?|hours?|hrs?)", s)
    if not m:
        return None
    count = _parse_count(m.group("n"))
    if count is None:
        return None
    unit = _UNIT_CANON[m.group("u")]
    word = unit + ("" if count == 1 else "s")
    return count * _UNIT_SECONDS[unit], f"{m.group('n')} {word}"


_LIST_REM = _re.compile(
    _LEAD + r"(?:(?:what|which)\s+(?:reminders?|timers?)\s+(?:do\s+i\s+have|are\s+(?:set|pending))|do\s+i\s+have\s+any\s+(?:reminders?|timers?)|(?:list|show|read)\s+(?:me\s+)?(?:(?:all|my|the)\s+)*(?:reminders?|timers?))[\s.!?,]*$",
    _re.IGNORECASE,
)
_CANCEL_REM = _re.compile(
    _LEAD + r"(?:cancel|clear|delete|remove)\s+(?:(?:all|my|the|any)\s+)*(?:reminders?|timers?)[\s.!?,]*$",
    _re.IGNORECASE,
)


def _route_reminder_admin(system, text: str):
    if _LIST_REM.match(text):
        args = {"action": "list"}
    elif _CANCEL_REM.match(text):
        low = text.lower()
        kind = ""
        if "timer" in low and "reminder" not in low:
            kind = "timer"
        elif "reminder" in low and "timer" not in low:
            kind = "reminder"
        args = {"action": "cancel", "kind": kind}
    else:
        return None
    executor = getattr(system, "tool_executor", None)
    if executor is None:
        return None
    try:
        import json as _json
        from openjarvis.core.types import ToolCall

        res = executor.execute(
            ToolCall(id="voice_route", name="set_reminder", arguments=_json.dumps(args))
        )
    except Exception:
        return None
    print(f"route: set_reminder({args}) -> success={res.success} | {res.content}")
    if str(res.content).startswith("Unknown tool"):
        return "Reminders aren't in my tool list yet, sir."
    if not res.success:
        return "I couldn't do that, sir."
    return str(res.content).rstrip(".") + ", sir."


def _try_route_reminder(system, text: str):
    _admin = _route_reminder_admin(system, text)
    if _admin is not None:
        return _admin
    kind, msg = None, ""
    m = _REMIND_IN.match(text) or _REMIND_TO_IN.match(text) or _REMIND_BARE.match(text)
    if m:
        kind, dur, msg = "reminder", m.group("dur"), (m.groupdict().get("msg") or "").strip()
    else:
        t = _TIMER.match(text)
        if t:
            kind, dur = "timer", (t.group("b") or t.group("a") or "")
        elif _REMIND_ANY.match(text):
            return "I can set a reminder like this, sir: remind me in ten minutes to check the oven."
        else:
            return None
    parsed = _parse_duration(dur)
    if parsed is None:
        if kind == "timer":
            return "I didn't catch how long, sir. Try: set a timer for five minutes."
        return "I didn't catch how long, sir. Try: remind me in ten minutes to check the oven."
    seconds, label = parsed
    if not 1 <= seconds <= 86400:
        return "I can set reminders from one second up to twenty-four hours, sir."
    executor = getattr(system, "tool_executor", None)
    if executor is None:
        return None
    message = (msg or "your time is up") if kind == "reminder" else f"Your timer for {label} is up, sir."
    try:
        import json as _json
        from openjarvis.core.types import ToolCall

        from openjarvis.tools.set_reminder import ROUTER_KEY, router_call

        with router_call():
            res = executor.execute(
                ToolCall(
                    id="voice_route",
                    name="set_reminder",
                    arguments=_json.dumps({"seconds": seconds, "message": message, "kind": kind, "_router_key": ROUTER_KEY}),
                )
            )
    except Exception:
        return None
    print(f"route: set_reminder({seconds:g}s, {kind}) -> success={res.success} | {res.content}")
    if str(res.content).startswith("Unknown tool"):
        return "Reminders aren't in my tool list yet, sir."
    if not res.success:
        return "I couldn't set that, sir."
    if kind == "timer":
        return f"Timer set for {label}, sir."
    if not msg:
        return f"I'll remind you in {label}, sir."
    return f"I'll remind you in {label} to {msg}, sir."


def _seconds_until_next_reminder():
    try:
        from openjarvis.tools.set_reminder import seconds_until_next

        wait = seconds_until_next()
    except Exception:
        return None
    return None if wait is None else max(1.0, wait)


def _speak_due_reminders(console, voice_session) -> None:
    try:
        from openjarvis.tools.set_reminder import due_reminders, remove_reminders

        due = due_reminders()
    except Exception:
        return
    done = []
    for r in due:
        late = time.time() - float(r["due"])
        text = r["message"] if r.get("kind") == "timer" else f"Reminder, sir: {r['message']}"
        if late > 90:
            text += " Sorry, that one is running late."
        console.print(f"[bold cyan]Reminder due:[/bold cyan] {text}")
        try:
            speak(_speech_clean(text), console, voice_session)
        except Exception as exc:
            console.print(f"[yellow]Could not speak the reminder: {exc}[/yellow]")
        done.append(r["id"])
    try:
        remove_reminders(done)
    except Exception as exc:
        console.print(f"[yellow]Could not clear reminders: {exc}[/yellow]")


_LATE_ECHO_SECS = 15.0


class _Jobs:
    """Runs one model call at a time on a background thread. The worker never
    speaks or prints: it queues its answer, and the main loop speaks it
    between wake-word listens, when the microphone stream is closed."""

    def __init__(self, system, wakeword) -> None:
        self._system = system
        self._wakeword = wakeword
        self._q = queue.Queue()
        self._thread = None

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, question: str) -> None:
        started = time.monotonic()

        def run() -> None:
            item = {"question": question}
            try:
                result = self._system.ask(question, voice=True)
                item["content"] = result.get("content", "")
            except Exception as exc:
                item["error"] = str(exc)
            item["secs"] = time.monotonic() - started
            self._q.put(item)
            wake = getattr(self._wakeword, "interrupt", None)
            if wake is not None:
                try:
                    wake()
                except Exception:
                    pass

        self._thread = threading.Thread(target=run, name="jarvis-model", daemon=True)
        self._thread.start()

    def finished(self) -> list:
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out


def _short_question(q: str, words: int = 8) -> str:
    parts = " ".join(q.split()).strip(" .!?,").split()
    if len(parts) > words:
        cut = parts[:words]
        while len(cut) > 3 and cut[-1].lower() in {"the", "and", "of", "a", "an", "to", "in", "on", "for", "is", "or"}:
            cut.pop()
        return " ".join(cut) + " and so on"
    return " ".join(parts)


def _speak_finished_answers(console, voice_session, jobs) -> None:
    for item in jobs.finished():
        console.print(f"[dim]timing: model answer took {item['secs']:.0f}s[/dim]")
        if item.get("error"):
            console.print(f"[red]Error: {item['error']}[/red]")
            text = "Sorry, sir, I couldn't answer that."
        else:
            body = _truncate_for_voice(_speech_clean(item.get("content", "")))
            if not body:
                body = "I didn't come up with an answer, sir."
            text = body
            if item["secs"] > _LATE_ECHO_SECS:
                q = _lower_question_lead(_short_question(item["question"]))
                if q:
                    text = _speech_clean(f"You asked, {q}.") + " " + body
        console.print(f"[bold]Jarvis:[/bold] {text}")
        try:
            speak(text, console, voice_session)
        except Exception as exc:
            console.print(f"[yellow]Could not speak the answer: {exc}[/yellow]")


_BRIEF_ASK = _re.compile(
    _LEAD + r"(?:good\s+(?:morning|afternoon|evening)(?:[\s,]+(?:jarvis|sir))?|brief\s+me|(?:give|read)\s+me\s+(?:my|the)\s+(?:(?:morning|daily)\s+)?brief(?:ing)?|(?:what(?:'s|\s+is)\s+)?(?:my|the)\s+(?:(?:morning|daily)\s+)?brief(?:ing)?|(?:morning|daily)\s+brief(?:ing)?)(?:\s+please)?[\s.!?,]*$",
    _re.IGNORECASE,
)
_QUESTION_LEADS = {
    "what", "what's", "whats", "how", "why", "who", "when", "where", "which",
    "can", "could", "would", "is", "are", "do", "does", "did", "tell", "explain",
    "give", "describe", "define", "show", "list", "write", "summarize", "compare",
}


def _lower_question_lead(q: str) -> str:
    """'What is meditation' -> 'what is meditation' for the spoken echo, but
    leave names like 'Spotify' alone."""
    first = q.split(" ", 1)[0].strip(",.?!").lower() if q else ""
    if first in _QUESTION_LEADS and q[:1].isupper():
        return q[0].lower() + q[1:]
    return q


def _brief_text(system) -> str:
    from datetime import datetime

    now = datetime.now()
    part = "morning" if now.hour < 12 else "afternoon" if now.hour < 18 else "evening"
    try:
        from num2words import num2words as _n2w

        day = _n2w(now.day, to="ordinal", lang="en_US")
    except Exception:
        day = str(now.day)
    text = f"Good {part}, sir. It is {now.strftime('%A, %B')} {day}."
    reminders = ""
    try:
        import json as _json
        from openjarvis.core.types import ToolCall

        executor = getattr(system, "tool_executor", None)
        if executor is not None:
            res = executor.execute(
                ToolCall(id="voice_brief", name="set_reminder", arguments=_json.dumps({"action": "list"}))
            )
            if res.success and not str(res.content).startswith("Unknown tool"):
                reminders = str(res.content)
    except Exception:
        reminders = ""
    if reminders.startswith("No reminders"):
        reminders = "You have no reminders set."
    return (text + " " + reminders).strip()


def _brief_state_path():
    from pathlib import Path

    return Path.home() / ".openjarvis" / "last_brief.txt"


def _speak_morning_brief(console, voice_session, system) -> None:
    """Speak the brief once per day, on the first start. Never blocks startup
    on an error. JARVIS_NO_BRIEF=1 turns it off."""
    import os
    from datetime import date

    if os.environ.get("JARVIS_NO_BRIEF"):
        return
    try:
        today = date.today().isoformat()
        path = _brief_state_path()
        try:
            if path.read_text(encoding="utf-8").strip() == today:
                return
        except Exception:
            pass
        text = _brief_text(system)
        console.print(f"[bold]Jarvis:[/bold] {text}")
        speak(_speech_clean(text), console, voice_session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(today, encoding="utf-8")
    except KeyboardInterrupt:
        return
    except Exception as exc:
        console.print(f"[yellow]Could not speak the brief: {exc}[/yellow]")


def _try_route_brief(system, text: str):
    if not _BRIEF_ASK.match(text):
        return None
    return _brief_text(system)


def _is_junk_transcript(text: str) -> bool:
    """One or two words that did not route as a command. These are almost always
    a clipped or misheard phrase ('Spotify.', 'For if me.'), and sending them to
    the model holds the single model slot for minutes."""
    return len(_re.findall(r"[A-Za-z0-9']+", text)) <= 2


_MIN_CONFIDENCE = 0.2  # exp(avg_logprob): the one mumbled probe scored 0.12, clear commands 0.40+


def _is_low_confidence(session) -> bool:
    c = getattr(session, "last_confidence", None)
    return c is not None and c < _MIN_CONFIDENCE


def _try_route_command(system, text: str):
    """Deterministic fast path for 'open X', 'close X' and 'switch to X'.

    Returns the sentence to speak, or None to fall through to the model.
    Calls go through system.tool_executor (not tool.execute directly) so
    the capability policy, rate limiter and audit log still apply.
    """
    _brf = _try_route_brief(system, text)
    if _brf is not None:
        return _brf
    _rem = _try_route_reminder(system, text)
    if _rem is not None:
        return _rem
    m = _ROUTE_VERBS.match(text)
    if not m:
        return None
    verb = " ".join(m.group(1).lower().split())
    app = m.group(2).strip()
    app = _re.sub(r"[\s,]+please$", "", app, flags=_re.IGNORECASE).strip()
    words = app.lower().split()
    if not words or len(words) > 3:
        return None
    # No model sits in between, so skip pronouns/filler and tiny fragments
    # ("close it", "open up") that would match a random window.
    if all(w in _ROUTE_IGNORE for w in words) or len("".join(words)) < 3:
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
    print(f"route: {tool_name}({app!r}) -> success={res.success} | {res.content}")
    msg = str(res.content)
    if res.success:
        if len(msg) <= 70:
            first, _, rest = msg.partition(". ")
            return first.rstrip(".") + ", sir." + (f" {rest}" if rest else "")
        return f"{past} {app}, sir."
    if msg.startswith("Nothing matching") or "is not an installed application" in msg:
        if tool_name == "open_application":
            return f"I couldn't find an app called {app}, sir."
        return f"{app} doesn't seem to be open, sir."
    if "matches multiple" in msg or "matches different" in msg:
        return f"More than one app matches {app}, sir. Please be more specific."
    return msg if len(msg) <= 90 else "I couldn't do that, sir."


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

    # Built lazily on the first screen request. Every SystemBuilder().build()
    # registers its own audit logger on the shared event bus, so building a
    # second system at startup made every audit row appear twice.
    vision_system = None

    _SCREEN_KEYWORDS = ("screen", "on my display", "what do you see", "look at this")

    voice_session = VoiceSession(config)

    # Load the speech models now so the first spoken reply is not the slow
    # one (Kokoro's first synthesis took 25-75 s). The audio is discarded;
    # any failure is ignored and models then load lazily as before.
    console.print("[dim]Warming up the voice (first start can take a minute)...[/dim]")
    try:
        voice_session.get_stt_backend()
        _tts = voice_session.get_tts_backend()
        if _tts is not None:
            _vid, _spd = voice_session.voice_for_backend(_tts)
            _kw = {"output_format": "wav", "speed": _spd}
            if _vid:
                _kw["voice_id"] = _vid
            _tts.synthesize("Ready.", **_kw)
    except Exception as exc:
        console.print(f"[dim yellow]Voice warm-up skipped: {exc}[/dim yellow]")
    wake_phrase = (wake_model or config.speech.wakeword_model).replace("_", " ")

    console.print(f"[green]Listening for the wake word ({wake_phrase!r})...[/green]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]")

    _speak_morning_brief(console, voice_session, system)
    jobs = _Jobs(system, wakeword)

    try:
        while True:
            _speak_due_reminders(console, voice_session)
            _speak_finished_answers(console, voice_session, jobs)
            _wait = _seconds_until_next_reminder()
            try:
                heard = wakeword.listen(timeout=_wait)
            except KeyboardInterrupt:
                break
            if not heard:
                continue

            console.print("\n[bold cyan]Wake word detected.[/bold cyan]")
            t_rec = time.perf_counter()
            text = record_voice(console, voice_session)
            console.print(f"[dim]timing: listen+transcribe {time.perf_counter() - t_rec:.1f}s[/dim]")
            _conf = getattr(voice_session, "last_confidence", None)
            if isinstance(text, str) and text and _conf is not None:
                console.print(f"[dim]confidence: {_conf:.2f}[/dim]")
            if text is VOICE_EXIT:
                break
            if not text:
                console.print(f"[green]Listening for the wake word ({wake_phrase!r})...[/green]")
                continue

            try:
                t_ask = time.perf_counter()
                _routed = _try_route_command(system, text)
                is_screen_request = _routed is None and any(
                    kw in text.lower() for kw in _SCREEN_KEYWORDS
                )
                if _routed is not None:
                    result = {"content": _routed}
                elif is_screen_request and jobs.busy():
                    result = {
                        "content": (
                            "I'm still working on your last question, sir. "
                            "Ask me about the screen once I'm done."
                        )
                    }
                elif is_screen_request:
                    try:
                        import base64 as _b64
                        from openjarvis.cli._screen import capture_screen_to_temp

                        _shot = capture_screen_to_temp()
                        with open(_shot, "rb") as _f:
                            _img_b64 = _b64.b64encode(_f.read()).decode("ascii")
                        if vision_system is None:
                            console.print("[dim]Loading the screen-check system (first use)...[/dim]")
                            vision_system = (
                                SystemBuilder().agent("native_openhands").model("gemma3:4b").build()
                            )
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
                    if _is_junk_transcript(text) or _is_low_confidence(voice_session):
                        result = {
                            "content": "I didn't catch that, sir. Please say the whole command again."
                        }
                    elif jobs.busy():
                        result = {"content": "I'm still working on your last question, sir."}
                    else:
                        jobs.start(text)
                        result = {"content": "On it, sir."}
                console.print(f"[dim]timing: agent {time.perf_counter() - t_ask:.1f}s[/dim]")
                content = _speech_clean(result.get("content", ""))
                if _routed is None:
                    content = _truncate_for_voice(content)
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
        if hasattr(system, "close") and not jobs.busy():
            try:
                system.close()
            except Exception:
                pass


__all__ = ["listen"]
