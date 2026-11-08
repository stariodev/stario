"""TTY span tree for interactive development (stdlib + ANSI).

Not a production log format. Finished request roots scroll into history as a
ledger of completed traces; open roots repaint in a live footer at the bottom
of the terminal. Each frame is a single write wrapped in the terminal's
synchronized-output markers (``\\x1b[?2026h`` / ``\\x1b[?2026l``): erase the
previous live block, print newly finished roots, redraw the live block.

Layout: one header line per span (status glyph, name, status code, and the
duration in one right-hand column), then its attributes as ``key=value``
pairs. Keys that share a dotted prefix share one row under a dim label.
Lines never exceed ``min(columns - 1, 100)`` cells, so the duration column
stays near the text on a wide terminal.

Accessibility: only the eight base ANSI colors plus bold and dim, so light
and dark themes both work. Dim marks structure (time, keys, rails) and never
names, values, durations, or tracebacks. ``NO_COLOR`` turns color off,
``TERM=dumb`` turns color and the live footer off, and an ASCII glyph set
replaces the Unicode one when the stream cannot encode UTF-8 or when
``STARIO_TRACERS_TTY_ASCII=1``. The Unicode set uses glyphs from the WGL4
repertoire (every common monospace font has them) except the braille spinner.

The erase is reflow-aware: it counts screen rows from the stored visible width
of every previous live line against the *current* raw terminal columns, so it
still reaches the first live row after a shrink and cannot touch scrollback.
The trailing ``\\x1b[J`` also clears a stray ``^C`` echo or a partial line left
on the cursor row.

While live and attached to ``sys.stdout``, ``sys.stdout`` and ``sys.stderr``
are replaced by a small pass-through proxy (``logging.StreamHandler`` streams
pointing at the originals are retargeted as well) so ``print`` and logging
interleave with the live region instead of corrupting it. Limits: raw file
descriptor writes, subprocess output, and handlers bound to other streams
bypass the proxy.

`TTYRenderer` is the pure span→string layer and the unit-test seam: no locks,
threads, or I/O.
"""

from __future__ import annotations

import codecs
import contextlib
import functools
import logging
import os
import shutil
import sys
import threading
import time
import unicodedata
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO, cast
from unicodedata import combining, east_asian_width
from uuid import UUID

from stario._terminal import RESET as _RESET
from stario._terminal import SGR, color_enabled, enable_vt_for_stream

from .core import Attributes, Span, TelemetryStats
from .spans import RecordedEvent, RecordedLink, RecordingSpan

# Shutdown waits this long for the render thread and for a write in flight.
_STOP_WAIT_S = 1.0
_REFRESH_INTERVAL_S = 0.125
_ELLIPSIS = "…"
_FRAME_NS = 125_000_000
_NAME_MIN = 8
_MAX_WIDTH = 100
_STAMP_MIN = 50
_DUR_W = 6
_LABEL_MAX = 20
_SIDE_MIN = 24
_SIDE_PAIR = 40
_MAX_BODY_LINES = 12
_MAX_VALUE_CHARS = 4000
_MAX_NESTED = 60
_NESTED_EDGE = 30
_PROMOTED_KEYS = (
    "request.method",
    "request.path",
    "response.status_code",
    "status_code",
)


@dataclass(frozen=True, slots=True)
class _Glyphs:
    done: str
    failed: str
    waiting: str
    orphan: str
    spinner: str
    event: str
    tee: str
    elbow: str
    pipe: str
    dash: str
    frame_top: str
    frame_mid: str
    frame_bot: str
    link: str
    sep: str
    ellipsis: str
    newline: str
    micro: str


_UNICODE = _Glyphs(
    done="●",
    failed="×",
    waiting="○",
    orphan="!",
    spinner="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    event="◦",
    tee="├",
    elbow="└",
    pipe="│",
    dash="─",
    frame_top="┌",
    frame_mid="│",
    frame_bot="└",
    link="→",
    sep=" · ",
    ellipsis=_ELLIPSIS,
    newline="↵",
    micro="µs",
)
_ASCII = _Glyphs(
    done="*",
    failed="x",
    waiting="o",
    orphan="!",
    spinner="|/-\\",
    event="+",
    tee="|",
    elbow="`",
    pipe="|",
    dash="-",
    frame_top="|",
    frame_mid="|",
    frame_bot="|",
    link="->",
    sep=", ",
    ellipsis="...",
    newline="\\n",
    micro="us",
)


def _terminal_size() -> tuple[int, int]:
    """Layout size (columns, rows); one-column margin so full-width lines do not wrap."""
    try:
        size = shutil.get_terminal_size((80, 24))
        return max(1, size.columns - 1), max(1, size.lines)
    except OSError:
        return 79, 24


def _terminal_columns() -> int:
    """Raw terminal columns (no margin); used by the reflow-aware erase."""
    try:
        return max(1, shutil.get_terminal_size((80, 24)).columns)
    except OSError:
        return 80


def _clock_ns() -> int:
    return time.time_ns()


def _isatty(stream: object) -> bool:
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except Exception:
        return False


@functools.cache
def _home_dir() -> str | None:
    try:
        home = str(Path.home())
    except (RuntimeError, KeyError, OSError):
        return None
    return home if len(home) > 1 else None


def _ansi_sequence_end(text: str, index: int) -> int | None:
    if text[index : index + 2] != "\033[":
        return None
    for end in range(index + 2, len(text)):
        if "@" <= text[end] <= "~":
            return end + 1
    return None


def _cell_width(ch: str) -> int:
    if ch.isascii():
        return 1
    if combining(ch):
        return 0
    return 2 if east_asian_width(ch) in {"F", "W"} else 1


def _visible_width(text: str) -> int:
    width = 0
    i = 0
    while i < len(text):
        if (end := _ansi_sequence_end(text, i)) is not None:
            i = end
            continue
        width += _cell_width(text[i])
        i += 1
    return width


def _clip_visible(text: str, max_width: int, ellipsis: str = _ELLIPSIS) -> str:
    if max_width <= 0:
        return ""
    if _visible_width(text) <= max_width:
        return text

    ellipsis_width = _visible_width(ellipsis)
    if max_width <= ellipsis_width:
        return ellipsis[:max_width]

    budget = max_width - ellipsis_width
    width = 0
    i = 0
    pieces: list[str] = []
    saw_ansi = False
    while i < len(text):
        if (end := _ansi_sequence_end(text, i)) is not None:
            pieces.append(text[i:end])
            saw_ansi = True
            i = end
            continue
        ch = text[i]
        ch_width = _cell_width(ch)
        if width + ch_width > budget:
            break
        pieces.append(ch)
        width += ch_width
        i += 1

    pieces.append(ellipsis)
    if saw_ansi:
        pieces.append(_RESET)
    return "".join(pieces)


def _cut(text: str, width: int) -> tuple[str, str]:
    """Split plain ``text`` after at most ``width`` cells, at a space when possible."""
    w = 0
    i = 0
    while i < len(text):
        cw = _cell_width(text[i])
        if w + cw > width:
            break
        w += cw
        i += 1
    if i == 0:
        i = 1
    cut = text.rfind(" ", 1, i + 1)
    if cut > 0:
        return text[:cut], text[cut + 1 :]
    # No space: break after a path or query separator before mid-word.
    soft = max(text.rfind(ch, 1, i) for ch in "/&?,")
    if soft > 0:
        return text[: soft + 1], text[soft + 1 :]
    return text[:i], text[i:]


def _wrap(text: str, width: int, rest: int | None = None) -> list[str]:
    """Wrap plain text; the first row gets ``width`` cells, later rows ``rest``."""
    first = max(1, width)
    later = max(1, rest if rest is not None else width)
    out: list[str] = []
    for para in text.split("\n"):
        s = para
        budget = first
        while _visible_width(s) > budget:
            head, s = _cut(s, budget)
            out.append(head)
            budget = later
        out.append(s)
        first = later
    return out


def _fmt_duration(ns: int, micro: str = "µs") -> str:
    if ns < 0:
        ns = 0
    if ns < 1_000_000:
        us = ns / 1e3
        return f"{us:.1f}{micro}" if us < 10 else f"{us:.0f}{micro}"

    ms = ns / 1e6
    if ms < 10:
        return f"{ms:.1f}ms"
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f}s"
    minutes, seconds = divmod(int(ms / 1000), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    size = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024
        if size < 1024 or unit == "TiB":
            break
    return f"{size:.1f}".removesuffix(".0") + unit


def _fmt_value(key: str, value: object, home: str | None) -> str:
    """Display text of one attribute value (before sanitizing)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if (
        isinstance(value, int)
        and value >= 0
        and (key == "bytes" or key.endswith("_bytes"))
    ):
        return _fmt_bytes(value)
    text = str(value)
    if (
        home
        and isinstance(value, str)
        and (
            text == home
            or text.startswith(home + os.sep)
            or (os.altsep is not None and text.startswith(home + os.altsep))
        )
    ):
        return "~" + text[len(home) :]
    return text


def _uuid_tail(uid: UUID) -> str:
    s = str(uid)
    return s[-8:] if len(s) >= 8 else s


def _sanitize(text: object, *, multiline: bool, newline: str = "↵") -> str:
    """Make a user-supplied string terminal-safe.

    Tabs become four spaces, ``\\r`` variants become ``\\n``, newlines are kept
    only where multi-line is allowed (else ``newline``), and every remaining
    control character becomes a literal ``\\xNN`` escape so user text can never
    emit terminal escapes or break width accounting.
    """
    s = str(text).replace("\t", "    ").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for ch in s:
        if ch == "\n":
            out.append("\n" if multiline else newline)
        elif unicodedata.category(ch) == "Cc":
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def _status_color(code: int) -> str:
    if 200 <= code < 300:
        return "green"
    if 300 <= code < 500:
        return "yellow"
    return "red"


def _status_styles(color: str) -> tuple[str, ...]:
    # Yellow is pale on light themes; bold keeps it readable.
    return (color, "bold") if color == "yellow" else (color,)


def _promoted(span: RecordingSpan) -> tuple[str, dict[str, Any]]:
    """Display name and renderable attributes after request-name promotion."""
    attrs = dict(span.attributes or {})
    method = attrs.get("request.method")
    path = attrs.get("request.path")
    if method is not None and path is not None:
        for key in _PROMOTED_KEYS:
            attrs.pop(key, None)
        if attrs.get("http.route") == path:
            del attrs["http.route"]
        return f"{method} {path}", attrs
    return span.name, attrs


def _status_code(span: RecordingSpan) -> int | None:
    for key in ("response.status_code", "status_code"):
        if not span.attributes or key not in span.attributes:
            continue
        try:
            return int(span.attributes[key])
        except (ValueError, TypeError):
            continue
    return None


class _LiveRegion:
    """Bookkeeping for the live block currently on screen.

    Stores the text, layout width, and the visible width of every live line so
    the next frame can compute how many *screen rows* to walk back at whatever
    the terminal width is then (reflow-aware erase).
    """

    __slots__ = ("_line_widths", "_text", "_width")

    def __init__(self) -> None:
        self._text: str | None = None
        self._width: int | None = None
        self._line_widths: list[int] = []

    @property
    def has_content(self) -> bool:
        return self._text is not None

    def matches(self, text: str, width: int) -> bool:
        return self._text == text and self._width == width

    def store(self, text: str, width: int) -> None:
        if not text.endswith("\n"):
            text += "\n"
        self._text = text
        self._width = width
        self._line_widths = [_visible_width(line) for line in text.splitlines()]

    def clear(self) -> None:
        self._text = None
        self._width = None
        self._line_widths = []

    def erase_seq(self, columns: int) -> str:
        """Sequence that returns the cursor to the first live row and clears below.

        Empty when no live block is on screen, so callers never wipe the line
        the cursor happens to be on (e.g. a partial proxy line or ``^C``).
        """
        if not self._line_widths:
            return ""
        cols = max(1, columns)
        rows = sum(max(1, -(-w // cols)) for w in self._line_widths)
        self.clear()
        return "\r" + (f"\x1b[{rows}A" if rows else "") + "\x1b[J"


class _LiveEntry:
    """One candidate line of a live root block (header or attribute line)."""

    __slots__ = ("content", "kind", "order", "parent", "pri", "seq", "span", "waiting")

    def __init__(
        self,
        pri: int,
        seq: int,
        kind: str,
        span: RecordingSpan,
        parent: int,
        order: int,
        waiting: bool = False,
        content: str = "",
    ) -> None:
        self.pri = pri
        self.seq = seq
        self.kind = kind
        self.span = span
        self.parent = parent
        self.order = order
        self.waiting = waiting
        self.content = content


class TTYRenderer:
    """Pure string rendering for span trees; no I/O, locks, or threads.

    Pass terminal width, a ``parent_id → children`` map, a fixed ``now_ns``
    (all open-span durations in one frame come from the same instant so a
    parent is never shorter than a child), whether ANSI color is on, and
    whether to draw with the ASCII glyph set.

    `history_block` renders one root as a ledger entry for scrollback;
    `live_text` renders the active path of open roots for the live footer.
    """

    __slots__ = ("_children", "_color", "_g", "_home", "_now", "_width")

    def __init__(
        self,
        width: int,
        children: dict[UUID, list[RecordingSpan]],
        *,
        now_ns: int,
        color: bool,
        ascii: bool = False,
    ) -> None:
        self._width = max(1, min(width, _MAX_WIDTH))
        self._children = children
        self._now = now_ns
        self._color = color
        self._g = _ASCII if ascii else _UNICODE
        self._home = _home_dir()

    # --------------------------------------------------------------
    # shared primitives

    def _styled(self, text: str, *styles: str) -> str:
        if not self._color or not text:
            return text
        codes = "".join(SGR[s] for s in styles if s in SGR)
        if not codes:
            return text
        return f"{codes}{text}{_RESET}"

    def _clip(self, text: str, width: int) -> str:
        return _clip_visible(text, width, self._g.ellipsis)

    def _fit(self, line: str) -> str:
        """Final form of an output line: clipped to the width, no trailing blanks."""
        return self._clip(line, self._width).rstrip(" ")

    def _flat(self, text: object) -> str:
        return _sanitize(text, multiline=False, newline=self._g.newline)

    def _duration_ns(self, span: RecordingSpan) -> int | None:
        if span.start_ns is None:
            return None
        if span.end_ns is not None:
            return span.end_ns - span.start_ns
        return self._now - span.start_ns

    def _open_children(self, span: RecordingSpan) -> list[RecordingSpan]:
        kids = [
            c
            for c in self._children.get(span.id, [])
            if c.start_ns is not None and c.in_progress
        ]
        kids.sort(key=lambda c: c.start_ns or 0)
        return kids

    def _marker(
        self, span: RecordingSpan, *, is_root: bool, orphan: bool, waiting: bool
    ) -> str:
        """Status glyph: the root's bullet, or the second cell of a child connector."""
        g = self._g
        if orphan:
            return self._styled(g.orphan, "yellow", "bold")
        if span.failed:
            return self._styled(g.failed, "red", "bold")
        if span.in_progress:
            if waiting:
                return self._styled(g.waiting, "cyan")
            frame = (self._now // _FRAME_NS) % len(g.spinner)
            return self._styled(g.spinner[frame], "cyan")
        if not is_root:
            return self._styled(g.dash, "dim")
        code = _status_code(span)
        return self._styled(g.done, *_status_styles(_status_color(code or 200)))

    def _is_hot(self, span: RecordingSpan, root: RecordingSpan) -> bool:
        """A finished child that took at least half of a root of 1 ms or more."""
        if not span.finished:
            return False
        total = self._duration_ns(root) or 0
        took = self._duration_ns(span) or 0
        return total >= 1_000_000 and took * 2 >= total

    def _header(
        self,
        span: RecordingSpan,
        *,
        lead: str,
        marker: str,
        is_root: bool,
        hot: bool = False,
        meta: str = "",
    ) -> str:
        """``{lead}{marker} {name}  {status}{meta} … {duration}`` in one line.

        The status code stays next to the name it belongs to; the duration is
        right-aligned so durations line up down a tree.
        """
        if span.start_ns is None:
            raise RuntimeError("TTYRenderer can only render started spans")
        width = self._width
        name, _ = _promoted(span)
        name_text = self._flat(name)
        if is_root:
            name_text = self._styled(name_text, "bold")
        head = lead + marker + " "
        code = _status_code(span)
        code_s = "" if code is None else str(code)
        dur = self._duration_ns(span)
        dur_s = "" if dur is None else _fmt_duration(dur, self._g.micro)

        # Narrowing: the name keeps >= 8 cells; drop the duration, then the status.
        budget = width - _visible_width(head)
        status_w = 2 + len(code_s) if code_s else 0
        dur_w = 2 + max(_DUR_W, len(dur_s)) if dur_s else 0
        if budget - status_w - dur_w < _NAME_MIN:
            dur_w = 0
        if budget - status_w - dur_w < _NAME_MIN:
            status_w = 0
        name_w = max(0, budget - status_w - dur_w)

        line = head + self._clip(name_text, name_w)
        if status_w and code is not None:
            line += "  " + self._styled(code_s, *_status_styles(_status_color(code)))
        # The meta suffix is appended whole or not at all — never mid-token.
        meta_w = _visible_width(meta)
        if meta_w and _visible_width(line) + meta_w <= width - dur_w:
            line += meta
        if dur_w:
            right = self._styled(dur_s.rjust(_DUR_W), "bold" if hot else "")
            line += " " * max(2, width - _visible_width(line) - _DUR_W) + right
        return self._clip(line, width)

    # --------------------------------------------------------------
    # attributes: key=value pairs, grouped by dotted prefix

    def _pair(self, key: str, value: object) -> tuple[str, str]:
        """Sanitized ``(key, value)``; the value keeps newlines for block layout."""
        text = _fmt_value(key, value, self._home)[:_MAX_VALUE_CHARS]
        return self._flat(key), _sanitize(text, multiline=True)

    def _quote(self, value: str) -> str:
        if value and " " not in value and '"' not in value:
            return value
        return '"' + value.replace('"', '\\"') + '"'

    def _token(self, key: str, value: str) -> str:
        return self._styled(key + "=", "dim") + value

    def _inline(self, attrs: dict[str, Any] | None) -> str:
        if not attrs:
            return ""
        tokens: list[str] = []
        for raw_key, raw_value in attrs.items():
            key, value = self._pair(raw_key, raw_value)
            tokens.append(
                self._token(key, self._quote(value.replace("\n", self._g.newline)))
            )
        return " ".join(tokens)

    def _capped(self, rows: list[str]) -> list[str]:
        if len(rows) <= _MAX_BODY_LINES:
            return rows
        more = len(rows) - _MAX_BODY_LINES
        note = self._styled(f"{self._g.ellipsis} {more} more lines", "dim")
        return [*rows[:_MAX_BODY_LINES], "  " + note]

    def _hanging(self, key: str, value: str, avail: int) -> list[str]:
        """One pair wider than the row: wrap it with a 2-cell hanging indent."""
        rows = _wrap(f"{key}={value}", avail, avail - 2)
        out = [rows[0], *("  " + row for row in rows[1:])]
        head = key + "="
        if out[0].startswith(head):
            out[0] = self._token(key, out[0][len(head) :])
        return self._capped(out)

    def _flow(
        self, pairs: list[tuple[str, str]], avail: int, *, live: bool
    ) -> list[str]:
        """Pack pairs into rows of ``avail`` cells.

        History wraps a long value (no information is lost) and shows a
        multi-line value as an indented block. The live footer clips both to
        one row so its row budget holds.
        """
        avail = max(1, avail)
        lines: list[str] = []
        cur: list[str] = []
        cur_w = 0

        def flush() -> None:
            nonlocal cur, cur_w
            if cur:
                lines.append(" ".join(cur))
                cur = []
                cur_w = 0

        for key, value in pairs:
            block = "\n" in value
            flat = self._quote(value.replace("\n", self._g.newline))
            width = _visible_width(key) + 1 + _visible_width(flat)
            if block and not live:
                flush()
                lines.append(self._styled(key + "=", "dim"))
                lines.extend(
                    self._capped(["  " + row for row in _wrap(value, avail - 2)])
                )
                continue
            if width > avail:
                flush()
                if live:
                    lines.append(self._clip(self._token(key, flat), avail))
                else:
                    lines.extend(self._hanging(key, flat, avail))
                continue
            if cur and cur_w + 1 + width <= avail:
                cur.append(self._token(key, flat))
                cur_w += 1 + width
            else:
                flush()
                cur = [self._token(key, flat)]
                cur_w = width
        flush()
        return lines

    def _attr_lines(self, span: RecordingSpan, avail: int, *, live: bool) -> list[str]:
        """Attribute rows: ungrouped pairs first, then one row group per prefix.

        The first segment of a dotted span name folds away (``server.host`` on
        ``server.startup`` shows as ``host``). Two or more keys that share the
        text before their last dot become a group: ``compression  gzip_level=1
        zstd_level=3``. A label sits beside its pairs when every pair (up to
        40 cells) fits there, else on its own row above them.
        """
        _, attrs = _promoted(span)
        if not attrs:
            return []
        fold = f"{span.name.split('.', 1)[0]}." if "." in span.name else None
        keyed: list[tuple[str, Any]] = []
        for raw_key, value in attrs.items():
            key = raw_key
            if fold and key.startswith(fold) and len(key) > len(fold):
                key = key[len(fold) :]
            keyed.append((key, value))
        counts = Counter(key.rpartition(".")[0] for key, _ in keyed)
        flat: list[tuple[str, str]] = []
        groups: dict[str, list[tuple[str, str]]] = {}
        for key, value in keyed:
            label, _, leaf = key.rpartition(".")
            if label and leaf and counts[label] >= 2:
                groups.setdefault(label, []).append(self._pair(leaf, value))
            else:
                flat.append(self._pair(key, value))

        lines = self._flow(flat, avail, live=live) if flat else []
        if not groups:
            return lines
        labels = {label: self._flat(label) for label in groups}
        label_w = min(_LABEL_MAX, max(_visible_width(t) for t in labels.values()))
        widest = max(
            _visible_width(k) + 1 + _visible_width(self._quote(v))
            for pairs in groups.values()
            for k, v in pairs
        )
        side = avail - label_w - 2 >= max(_SIDE_MIN, min(widest, _SIDE_PAIR))
        for label, pairs in groups.items():
            if side:
                rows = self._flow(pairs, avail - label_w - 2, live=live)
                text = self._clip(labels[label], label_w)
                pad = " " * (label_w - _visible_width(text) + 2)
                lines.append(self._styled(text, "dim") + pad + rows[0])
                lines.extend(" " * (label_w + 2) + row for row in rows[1:])
            else:
                lines.append(self._styled(self._clip(labels[label], avail), "dim"))
                lines.extend(
                    "  " + row for row in self._flow(pairs, avail - 2, live=live)
                )
        return lines

    def _link_line(self, link: RecordedLink, prefix: str) -> str:
        text = f"{self._g.link} {self._flat(link.name)} {_uuid_tail(link.span_id)}"
        line = prefix + self._styled(text, "dim")
        if link.attributes:
            line += "  " + self._inline(link.attributes)
        return line

    # --------------------------------------------------------------
    # body blocks (errors, frames, event bodies)

    def _frame_lines(self, body: str, prefix: str) -> list[str]:
        """Framed traceback; long rows wrap (2-cell continuation indent), never clip."""
        text = _sanitize(body, multiline=True).rstrip("\n")
        # Budget after "{prefix}{edge} " minus the continuation indent.
        avail = max(1, self._width - _visible_width(prefix) - 4)
        rows: list[str] = []
        for line in text.split("\n"):
            wrapped = _wrap(line, avail)
            rows.append(wrapped[0])
            rows.extend("  " + piece for piece in wrapped[1:])
        g = self._g
        n = len(rows)
        out: list[str] = []
        for i, row in enumerate(rows):
            edge = (
                g.frame_top if i == 0 else (g.frame_bot if i == n - 1 else g.frame_mid)
            )
            out.append(prefix + self._styled(edge, "dim") + " " + row)
        return out

    def _body_lines(self, body: str, prefix: str) -> list[str]:
        """Event body; wraps instead of clipping, capped at _MAX_BODY_LINES wrapped rows."""
        text = _sanitize(body, multiline=True).rstrip("\n")
        avail = max(1, self._width - _visible_width(prefix))
        rows = _wrap(text, avail)
        out = [prefix + row for row in rows[:_MAX_BODY_LINES]]
        if len(rows) > _MAX_BODY_LINES:
            more = len(rows) - _MAX_BODY_LINES
            out.append(
                prefix + self._styled(f"{self._g.ellipsis} {more} more lines", "dim")
            )
        return out

    # --------------------------------------------------------------
    # history blocks

    def history_block(self, root: RecordingSpan) -> str:
        """Render one root (and its whole subtree) for scrollback."""
        if root.start_ns is None:
            raise RuntimeError("TTYRenderer can only render started spans")
        stamp = ""
        if self._width >= _STAMP_MIN:
            clock = datetime.fromtimestamp(root.start_ns / 1e9).strftime("%H:%M:%S")
            stamp = self._styled(clock, "dim") + " "
        lines: list[str] = []
        self._emit_span(
            lines,
            root,
            lead=stamp,
            rail=" " * _visible_width(stamp),
            is_root=True,
            root=root,
        )
        return "\n".join(self._fit(line) for line in lines)

    def _consumed_exception(self, span: RecordingSpan) -> RecordedEvent | None:
        """The exception event folded into the error line of a failed span."""
        if not span.failed:
            return None
        for event in span.events or []:
            if (
                event.name == "exception"
                and event.attributes
                and event.attributes.get("exc.message") == span.error
            ):
                return event
        return None

    def _nested_items(
        self, span: RecordingSpan, exclude: RecordedEvent | None
    ) -> list[tuple[int, RecordedEvent | RecordingSpan]]:
        return sorted(
            chain(
                ((e.time_ns, e) for e in span.events or [] if e is not exclude),
                (
                    (c.start_ns, c)
                    for c in self._children.get(span.id, [])
                    if c.start_ns is not None
                ),
            ),
            key=lambda x: x[0],
        )

    def _emit_span(
        self,
        lines: list[str],
        span: RecordingSpan,
        *,
        lead: str,
        rail: str,
        is_root: bool,
        root: RecordingSpan,
    ) -> None:
        """Header, body, and nested items of one span.

        ``lead`` is the styled text before the status glyph; ``rail`` is the
        plain prefix of this span's nested connectors (the root's sits under
        its glyph, a child's under its name).
        """
        orphan = root.end_ns is not None and span.in_progress
        waiting = any(c.in_progress for c in self._children.get(span.id, []))
        consumed = self._consumed_exception(span)
        nested = self._nested_items(span, consumed)
        marker = self._marker(span, is_root=is_root, orphan=orphan, waiting=waiting)
        hot = not is_root and self._is_hot(span, root)
        lines.append(
            self._header(span, lead=lead, marker=marker, is_root=is_root, hot=hot)
        )
        body = self._styled(rail + (self._g.pipe if nested else " "), "dim") + " "
        avail = self._width - _visible_width(body)
        if span.failed:
            error = span.error or "failed"
            exc_type = (
                consumed.attributes.get("exc.type")
                if consumed is not None and consumed.attributes
                else None
            )
            text = f"{exc_type}: {error}" if exc_type else error
            lines.append(body + self._styled(self._flat(text), "red"))
            if consumed is not None and consumed.body:
                lines.extend(self._frame_lines(consumed.body, body))
        if orphan:
            lines.append(
                body
                + self._styled("still open when the parent ended", "yellow", "bold")
            )
        lines.extend(body + line for line in self._attr_lines(span, avail, live=False))
        for link in span.links or []:
            lines.append(self._link_line(link, body))
        self._emit_nested(lines, nested, rail, root)

    def _emit_nested(
        self,
        lines: list[str],
        items: list[tuple[int, RecordedEvent | RecordingSpan]],
        rail: str,
        root: RecordingSpan,
    ) -> None:
        g = self._g
        n = len(items)
        marker: str | None = None
        shown: list[tuple[int, RecordedEvent | RecordingSpan] | None]
        if n > _MAX_NESTED:
            mid = items[_NESTED_EDGE : n - _NESTED_EDGE]
            spans_n = sum(1 for _, it in mid if isinstance(it, RecordingSpan))
            events_n = len(mid) - spans_n
            marker = (
                f"{g.ellipsis} {len(mid)} more ({spans_n} spans, {events_n} events)"
            )
            shown = [*items[:_NESTED_EDGE], None, *items[n - _NESTED_EDGE :]]
        else:
            shown = list(items)
        last = len(shown) - 1
        for i, entry in enumerate(shown):
            is_last = i == last
            lead = self._styled(rail + (g.elbow if is_last else g.tee), "dim")
            inner = rail + ("   " if is_last else g.pipe + "  ")
            if entry is None:
                lines.append(self._styled(rail + g.pipe + "  " + (marker or ""), "dim"))
                continue
            _, item = entry
            if isinstance(item, RecordedEvent):
                self._emit_event(lines, item, lead=lead, rail=inner, root=root)
            else:
                self._emit_span(
                    lines, item, lead=lead, rail=inner, is_root=False, root=root
                )

    def _emit_event(
        self,
        lines: list[str],
        event: RecordedEvent,
        *,
        lead: str,
        rail: str,
        root: RecordingSpan,
    ) -> None:
        text = self._flat(event.name)
        attrs = event.attributes
        if event.name == "exception":
            attrs = dict(event.attributes or {})
            exc_type = attrs.pop("exc.type", None)
            exc_msg = attrs.pop("exc.message", None)
            label = (
                f"{exc_type}: {exc_msg}"
                if exc_type and exc_msg
                else str(exc_msg or exc_type or "")
            )
            if label:
                text += "  " + self._styled(self._flat(label), "red")
        if inline := self._inline(attrs):
            text += "  " + inline
        offset = (
            f"+{_fmt_duration(event.time_ns - (root.start_ns or 0), self._g.micro)}"
        )
        lines.append(
            self._event_line(
                lead + self._styled(self._g.event, "dim") + " ", text, offset
            )
        )
        if event.body:
            body = self._styled(rail, "dim") + "  "
            if event.name == "exception":
                lines.extend(self._frame_lines(event.body, body))
            else:
                lines.extend(self._body_lines(event.body, body))

    def _event_line(self, head: str, text: str, offset: str) -> str:
        """``{head}{text} … {offset}``, the offset right-aligned in the duration column."""
        width = self._width
        budget = width - _visible_width(head) - 2 - _DUR_W
        if budget < _NAME_MIN:
            return self._clip(head + text, width)
        line = head + self._clip(text, budget)
        right = self._styled(offset.rjust(_DUR_W), "dim")
        gap = width - _visible_width(line) - _visible_width(right)
        return self._clip(line + " " * max(2, gap) + right, width)

    # --------------------------------------------------------------
    # live area

    def live_text(self, open_roots: list[RecordingSpan], *, max_rows: int) -> str:
        roots = sorted(open_roots, key=lambda s: s.start_ns or 0)
        if not roots or max_rows <= 0:
            return ""
        newest_first = list(reversed(roots))
        if max_rows == 1:
            entries = [_LiveEntry(0, 0, "header", newest_first[0], -1, 0)]
            rendered = self._render_live_root(entries, {0})
            return self._fit(rendered[0]) if rendered else ""

        budget = max_rows - 1  # first row is the "── N open" line
        shown: list[RecordingSpan] = []
        for span in newest_first:
            if len(shown) >= budget:
                break
            shown.append(span)
        shown.reverse()  # display oldest at top
        not_shown = len(roots) - len(shown)

        per_root = {id(r): self._live_root_entries(r) for r in shown}
        grants = {id(r): 1 for r in shown}
        rows_left = budget - len(shown)
        for span in newest_first:
            if rows_left <= 0:
                break
            entries = per_root.get(id(span))
            if entries is None:
                continue
            grant = min(len(entries) - 1, rows_left)
            grants[id(span)] += grant
            rows_left -= grant

        lines = [self._open_line(len(roots), not_shown)]
        for span in shown:
            entries = per_root[id(span)]
            ordered = sorted(entries, key=lambda e: (e.pri, e.seq))
            kept = {e.seq for e in ordered[: grants[id(span)]]}
            lines.extend(self._render_live_root(entries, kept))
        return "\n".join(self._fit(line) for line in lines)

    def _open_line(self, n_open: int, not_shown: int) -> str:
        g = self._g
        text = f"{g.dash * 2} {n_open} open"
        if not_shown:
            text += f"{g.sep}{not_shown} not shown"
        pad = max(0, self._width - _visible_width(text) - 1)
        return self._clip(self._styled(text + " " + g.dash * pad, "dim"), self._width)

    def _live_meta(self, span: RecordingSpan) -> str:
        done = failed = 0
        for child in self._children.get(span.id, []):
            if not child.finished:
                continue
            if child.failed:
                failed += 1
            else:
                done += 1
        sep = self._g.sep
        meta = ""
        if done:
            meta += self._styled(f"{sep}{done} done", "dim")
        if failed:
            meta += self._styled(f"{sep}{failed} failed", "red")
        if span.events:
            meta += self._styled(f"{sep}{len(span.events)} events", "dim")
        return meta

    def _live_root_entries(self, root: RecordingSpan) -> list[_LiveEntry]:
        """Candidate lines of one live root, tagged by display priority."""
        entries: list[_LiveEntry] = []

        def add(
            pri: int,
            kind: str,
            span: RecordingSpan,
            parent: int,
            order: int,
            waiting: bool = False,
            content: str = "",
        ) -> int:
            entries.append(
                _LiveEntry(
                    pri, len(entries), kind, span, parent, order, waiting, content
                )
            )
            return len(entries) - 1

        def attr_entries(span: RecordingSpan, owner: int, depth: int, pri: int) -> None:
            # Prefix is the owner's rail (3 cells/level) + 2 cells; content packs
            # against that width whichever rail survives the row budget.
            avail = self._width - 3 * depth - 2
            for line in self._attr_lines(span, avail, live=True):
                add(pri, "attr", span, owner, 0, content=line)

        root_idx = add(
            0, "header", root, -1, 0, waiting=bool(self._open_children(root))
        )
        attr_entries(root, root_idx, 0, 4)

        def walk(span: RecordingSpan, parent: int, depth: int) -> None:
            for i, child in enumerate(self._open_children(span)):
                kids = self._open_children(child)
                waiting = bool(kids)
                idx = add(
                    2 if waiting else 1, "header", child, parent, i, waiting=waiting
                )
                if not waiting:
                    attr_entries(child, idx, depth, 3)
                walk(child, idx, depth + 1)

        walk(root, root_idx, 1)
        return entries

    def _render_live_root(self, entries: list[_LiveEntry], kept: set[int]) -> list[str]:
        """Emit kept entries in DFS order, rails recomputed over the kept set."""
        present: set[int] = set()
        for i in kept:
            j = i
            while j >= 0 and j not in present:
                present.add(j)
                j = entries[j].parent
        last_present: dict[int, int] = {}
        has_present_child: set[int] = set()
        for e in entries:
            if e.kind != "header" or e.parent < 0 or e.seq not in present:
                continue
            if e.order > last_present.get(e.parent, -1):
                last_present[e.parent] = e.order
            has_present_child.add(e.parent)

        lines: list[str] = []
        for e in entries:
            if e.seq not in kept:
                continue
            if e.kind == "header":
                rail, _ = self._live_rail(e, entries, last_present)
                is_root = e.parent < 0
                marker = self._marker(
                    e.span, is_root=is_root, orphan=False, waiting=e.waiting
                )
                lines.append(
                    self._header(
                        e.span,
                        lead=self._styled(rail, "dim"),
                        marker=marker,
                        is_root=is_root,
                        meta=self._live_meta(e.span),
                    )
                )
            else:
                owner = entries[e.parent]
                _, cont = self._live_rail(owner, entries, last_present)
                pipe = self._g.pipe if owner.seq in has_present_child else " "
                lines.append(self._styled(cont + pipe, "dim") + " " + e.content)
        return lines

    def _live_rail(
        self,
        entry: _LiveEntry,
        entries: list[_LiveEntry],
        last_present: dict[int, int],
    ) -> tuple[str, str]:
        """(rail, continuation) for an entry, recomputed over present siblings."""
        if entry.parent < 0:
            return "", ""
        g = self._g
        chain_: list[_LiveEntry] = []
        node = entry
        while node.parent >= 0:
            chain_.append(node)
            node = entries[node.parent]
        parts = [
            "   " if a.order == last_present.get(a.parent, -2) else g.pipe + "  "
            for a in reversed(chain_[1:])
        ]
        ancestor = "".join(parts)
        is_last = entry.order == last_present.get(entry.parent, -2)
        rail = ancestor + (g.elbow if is_last else g.tee)
        cont = ancestor + ("   " if is_last else g.pipe + "  ")
        return rail, cont


class _Passthrough:
    """``sys.stdout``/``sys.stderr`` proxy that yields the live block to plain prints.

    Complete lines are written through after erasing the live block; the next
    render tick repaints it. A ``flush()`` with a partial line (pdb/input
    prompt) pauses the render thread until a later newline write.
    """

    def __init__(
        self,
        original: TextIO,
        lock: threading.Lock,
        erase: Callable[[], None],
        set_paused: Callable[[bool], None],
    ) -> None:
        self._original = original
        self._lock = lock
        self._erase = erase
        self._set_paused = set_paused
        self._buf = ""
        self.encoding = getattr(original, "encoding", None)
        self.errors = getattr(original, "errors", None)

    @property
    def original(self) -> TextIO:
        return self._original

    def write(self, s: str) -> int:
        with self._lock:
            self._buf += s
            if "\n" in self._buf:
                cut = self._buf.rfind("\n") + 1
                complete, self._buf = self._buf[:cut], self._buf[cut:]
                self._erase()
                self._original.write(complete)
                self._original.flush()
                if not self._buf:
                    self._set_paused(False)
        return len(s)

    def writelines(self, lines: Iterable[str]) -> None:
        for s in lines:
            self.write(s)

    def flush(self) -> None:
        with self._lock:
            if self._buf:
                self._erase()
                self._original.write(self._buf)
                self._buf = ""
                self._original.flush()
                self._set_paused(True)
            else:
                self._original.flush()

    def drain(self) -> None:
        """Teardown: write a remaining partial line (plus newline) out of the way.

        Gives up after `_STOP_WAIT_S` when a write holds the lock.
        """
        if not self._lock.acquire(timeout=_STOP_WAIT_S):
            return
        try:
            if self._buf:
                self._original.write(self._buf + "\n")
                self._buf = ""
            self._original.flush()
        finally:
            self._lock.release()

    def isatty(self) -> bool:
        return _isatty(self._original)

    def fileno(self) -> int:
        return self._original.fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


class TTYTracer:
    """Live span tree on a TTY while `with tracer` is active.

    A background thread repaints open roots every 125 ms; `on_end` of a root
    only detaches the subtree into a pending queue — the render thread (or
    `flush`) formats history off the request thread. Spans get
    ``span.mutex = self._lock`` so mutations serialize with rendering.
    `stats()` is always zero (dev-only sink).

    ``live`` defaults to "the stream is a TTY and ``TERM`` is not ``dumb``".
    ``ascii`` defaults to "the stream cannot encode UTF-8".
    """

    __slots__ = (
        "_ascii",
        "_children",
        "_color",
        "_deferred",
        "_err_proxy",
        "_last_multiline",
        "_live_enabled",
        "_lock",
        "_open_span_ids",
        "_out",
        "_out_proxy",
        "_paused",
        "_pending",
        "_prompt_paused",
        "_region",
        "_retargeted",
        "_roots",
        "_running",
        "_thread",
        "_write_enc",
        "_write_lock",
    )

    def __init__(
        self,
        *,
        out: TextIO | None = None,
        live: bool | None = None,
        ascii: bool | None = None,
    ) -> None:
        self._out = out if out is not None else sys.stdout
        # A dumb terminal (Emacs shell, some CI consoles) has no cursor moves
        # and no color; it gets plain history lines.
        is_tty = _isatty(self._out) and os.environ.get("TERM") != "dumb"
        self._live_enabled = is_tty if live is None else bool(live)
        self._color = color_enabled() and is_tty
        self._last_multiline: bool | None = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._roots: dict[UUID, RecordingSpan] = {}
        self._children: dict[UUID, list[RecordingSpan]] = {}
        self._open_span_ids: set[UUID] = set()
        self._pending: deque[tuple[RecordingSpan, dict[UUID, list[RecordingSpan]]]] = (
            deque()
        )
        self._deferred: list[str] = []
        self._region = _LiveRegion()
        self._paused = False
        self._running = False
        self._thread: threading.Thread | None = None
        self._out_proxy: _Passthrough | None = None
        self._err_proxy: _Passthrough | None = None
        self._retargeted: list[tuple[logging.StreamHandler[TextIO], TextIO]] = []
        self._prompt_paused = False
        # Non-UTF output streams get lossy-encoded copies of every write so a
        # CJK glyph or box edge can never raise UnicodeEncodeError.
        try:
            resolved = codecs.lookup(
                getattr(self._out, "encoding", None) or "utf-8"
            ).name
        except (LookupError, TypeError):
            resolved = "utf-8"
        self._write_enc = None if resolved.startswith("utf") else resolved
        self._ascii = self._write_enc is not None if ascii is None else bool(ascii)

    def _renderer(
        self, width: int, children: dict[UUID, list[RecordingSpan]], now: int
    ) -> TTYRenderer:
        return TTYRenderer(
            width, children, now_ns=now, color=self._color, ascii=self._ascii
        )

    def __enter__(self) -> TTYTracer:
        self._start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._stop()

    def create(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        parent: Span | None = None,
    ) -> RecordingSpan:
        if not self._running:
            raise RuntimeError("TTYTracer must be entered before creating spans.")
        span = RecordingSpan.create(self, name, attributes, parent=parent)
        with self._lock:
            # Any span activity resumes a paused live region (e.g. after a
            # pdb prompt, where the newline echo bypasses the proxy).
            self._paused = False
            span.mutex = self._lock
            if span.parent_id is None:
                self._roots[span.id] = span
                self._open_span_ids.add(span.id)
            else:
                if span.parent_id not in self._open_span_ids:
                    raise RuntimeError(
                        "Cannot create a child span for a span that is not open."
                    )
                self._children.setdefault(span.parent_id, []).append(span)
                self._open_span_ids.add(span.id)
        return span

    def on_end(self, span: Span) -> None:
        span = cast(RecordingSpan, span)
        with self._lock:
            self._paused = False
            self._open_span_ids.discard(span.id)
            if span.parent_id is not None:
                return
            root = self._roots.pop(span.id, None)
            if root is None:
                return
            subtree: dict[UUID, list[RecordingSpan]] = {}
            self._detach_locked(span.id, subtree)
            self._pending.append((root, subtree))

    def stats(self) -> TelemetryStats:
        return TelemetryStats()

    def _detach_locked(
        self, span_id: UUID, out: dict[UUID, list[RecordingSpan]]
    ) -> None:
        children = self._children.pop(span_id, [])
        out[span_id] = children
        for child in children:
            self._open_span_ids.discard(child.id)
            self._detach_locked(child.id, out)

    # --------------------------------------------------------------
    # lifecycle

    def _start(self) -> None:
        if self._out is sys.stdout or _isatty(self._out):
            enable_vt_for_stream(self._out)
        with self._lock:
            if self._running:
                return
            self._running = True
        if self._live_enabled and self._out is sys.stdout:
            self._install_passthrough()
        with self._lock:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _stop(self) -> None:
        with self._lock:
            self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=_STOP_WAIT_S)
            self._thread = None
        self.flush()
        for proxy in (self._out_proxy, self._err_proxy):
            if proxy is not None:
                proxy.drain()
        self._restore_streams()

    def flush(self) -> None:
        """Write the pending history and every open root as finished.

        Skips the write when the write lock stays held for `_STOP_WAIT_S` (the
        render thread stuck in a terminal write), so shutdown cannot hang.
        """
        with self._lock:
            width, _ = _terminal_size()
            now = _clock_ns()
            blocks: list[str] = []
            while self._pending:
                root, kids = self._pending.popleft()
                try:
                    blocks.append(self._renderer(width, kids, now).history_block(root))
                except Exception as exc:
                    blocks.append(self._fallback_block(root, width, exc))
            remaining = sorted(
                (s for s in self._roots.values() if s.started),
                key=lambda s: s.start_ns or 0,
            )
            renderer = self._renderer(width, self._children, now)
            for span in remaining:
                try:
                    blocks.append(renderer.history_block(span))
                except Exception as exc:
                    blocks.append(self._fallback_block(span, width, exc))
            self._roots.clear()
            self._children.clear()
            self._open_span_ids.clear()
        self._write_frame(blocks, None, width, force=True, timeout=_STOP_WAIT_S)

    # --------------------------------------------------------------
    # render thread

    def _should_render_locked(self) -> bool:
        if self._paused:
            return False
        if self._pending or self._region.has_content:
            return True
        return any(span.started and span.in_progress for span in self._roots.values())

    def _loop(self) -> None:
        while True:
            started_at = time.perf_counter()
            with self._lock:
                if not self._running:
                    return
                should_render = self._should_render_locked()
            if should_render:
                with contextlib.suppress(Exception):
                    self._render()

            if (
                remaining := _REFRESH_INTERVAL_S - (time.perf_counter() - started_at)
            ) > 0:
                time.sleep(remaining)

    def _render(self) -> None:
        if self._paused:
            return
        with self._lock:
            blocks, live_text, width = self._render_plan_locked()
        self._write_frame(blocks, live_text, width)

    def _render_plan_locked(self) -> tuple[list[str], str | None, int]:
        width, rows = _terminal_size()
        now = _clock_ns()
        blocks: list[str] = []
        while self._pending:
            root, kids = self._pending.popleft()
            try:
                blocks.append(self._renderer(width, kids, now).history_block(root))
            except Exception as exc:
                blocks.append(self._fallback_block(root, width, exc))
        live_text: str | None = None
        if self._live_enabled:
            open_roots = sorted(
                (s for s in self._roots.values() if s.started and s.in_progress),
                key=lambda s: s.start_ns or 0,
            )
            if open_roots:
                try:
                    live_text = self._renderer(width, self._children, now).live_text(
                        open_roots, max_rows=max(1, rows // 2)
                    )
                except Exception as exc:
                    live_text = _clip_visible(
                        f"live view failed: {type(exc).__name__}", width
                    )
        return blocks, live_text, width

    def _fallback_block(self, span: RecordingSpan, width: int, exc: Exception) -> str:
        """One-line substitute for a root whose render raised — never drop a trace."""
        glyphs = _ASCII if self._ascii else _UNICODE
        line = _sanitize(span.name, multiline=False, newline=glyphs.newline)
        line += f"  (render failed: {type(exc).__name__})"
        if width >= _STAMP_MIN and span.start_ns is not None:
            stamp = datetime.fromtimestamp(span.start_ns / 1e9).strftime("%H:%M:%S")
            if self._color:
                stamp = f"{SGR['dim']}{stamp}{_RESET}"
            line = stamp + " " + line
        return _clip_visible(line, min(width, _MAX_WIDTH), glyphs.ellipsis)

    # --------------------------------------------------------------
    # writes (under _write_lock only; never takes _lock)

    def _write_frame(
        self,
        blocks: list[str],
        live_text: str | None,
        width: int,
        *,
        force: bool = False,
        timeout: float = -1,
    ) -> None:
        if not self._write_lock.acquire(timeout=timeout):
            return
        try:
            if self._paused:
                if not force:
                    self._deferred.extend(blocks)
                    return
                self._paused = False
            if self._deferred:
                blocks = self._deferred + blocks
                self._deferred = []
            blocks = self._spaced(blocks)
            if not self._live_enabled:
                if blocks:
                    self._out.write(self._to_out("\n".join(blocks) + "\n"))
                    self._out.flush()
                return
            live_text = live_text or ""
            if live_text and not live_text.endswith("\n"):
                live_text += "\n"
            if not blocks and self._region.matches(live_text, width):
                return
            if not blocks and not live_text and not self._region.has_content:
                return
            parts = ["\x1b[?2026h"]
            if self._prompt_paused:
                # Resumed after a partial-line prompt (pdb/input): step onto a
                # fresh line so nothing lands next to the prompt text.
                parts.append("\n")
                self._prompt_paused = False
            parts.append(self._region.erase_seq(_terminal_columns()))
            if blocks:
                parts.append("\n".join(blocks) + "\n")
            if live_text:
                parts.append(live_text)
            parts.append("\x1b[?2026l")
            self._out.write(self._to_out("".join(parts)))
            self._out.flush()
            if live_text:
                self._region.store(live_text, width)
        finally:
            self._write_lock.release()

    def _spaced(self, blocks: list[str]) -> list[str]:
        """Put a blank line before and after every block of several lines.

        One-line roots (plain requests) stack like an access log; a tree
        stands apart from its neighbors.
        """
        out: list[str] = []
        for block in blocks:
            multiline = "\n" in block
            if self._last_multiline is not None and (multiline or self._last_multiline):
                out.append("")
            out.append(block)
            self._last_multiline = multiline
        return out

    def _to_out(self, text: str) -> str:
        """Round-trip through the output encoding when it cannot hold text."""
        enc = self._write_enc
        if enc is None:
            return text
        return text.encode(enc, "replace").decode(enc)

    # --------------------------------------------------------------
    # stdout/stderr pass-through

    def _erase_live(self) -> None:
        """Erase the on-screen live block via ``_out`` (the TTY stream).

        Proxies call this before writing their own text so cursor escapes
        never land in a redirected stderr.
        """
        seq = self._region.erase_seq(_terminal_columns())
        if seq:
            self._out.write(self._to_out(seq))
            self._out.flush()

    def _set_paused(self, paused: bool) -> None:
        self._paused = paused
        # Remember pauses caused by a partial-line flush: the newline that ends
        # the prompt is echoed by the terminal, not written through the proxy,
        # so the next resumed frame must start on a fresh line. Cleared by a
        # proxy newline write or once the resume frame has been emitted.
        self._prompt_paused = paused

    def _install_passthrough(self) -> None:
        orig_out = self._out
        orig_err = sys.stderr
        out_proxy = _Passthrough(
            orig_out, self._write_lock, self._erase_live, self._set_paused
        )
        self._out_proxy = out_proxy
        sys.stdout = cast(TextIO, out_proxy)
        proxies: dict[int, _Passthrough] = {id(orig_out): out_proxy}
        # A redirected stderr must stay untouched and escape-free.
        if _isatty(orig_err):
            err_proxy = _Passthrough(
                orig_err, self._write_lock, self._erase_live, self._set_paused
            )
            self._err_proxy = err_proxy
            sys.stderr = cast(TextIO, err_proxy)
            proxies[id(orig_err)] = err_proxy
        loggers: list[logging.Logger] = [logging.root]
        loggers += [
            logger
            for logger in logging.Logger.manager.loggerDict.values()
            if isinstance(logger, logging.Logger)
        ]
        for logger in loggers:
            for handler in logger.handlers:
                if not isinstance(handler, logging.StreamHandler):
                    continue
                stream_handler = cast("logging.StreamHandler[TextIO]", handler)
                proxy = proxies.get(id(stream_handler.stream))
                if proxy is not None:
                    self._retargeted.append((stream_handler, stream_handler.stream))
                    stream_handler.setStream(cast(TextIO, proxy))

    def _restore_streams(self) -> None:
        for handler, stream in self._retargeted:
            with contextlib.suppress(Exception):
                handler.setStream(stream)
        self._retargeted.clear()
        if self._out_proxy is not None and sys.stdout is self._out_proxy:
            sys.stdout = self._out_proxy.original
        if self._err_proxy is not None and sys.stderr is self._err_proxy:
            sys.stderr = self._err_proxy.original
        self._out_proxy = None
        self._err_proxy = None
