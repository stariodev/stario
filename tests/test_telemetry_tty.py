"""TTY tracer (Ledger) tests.

Formatting is verified through `TTYRenderer` on hand-built `RecordingSpan`
records with a fixed `now_ns`. Tracer lifecycle and live I/O drive
`TTYTracer._render()` with `_running=True` and monkeypatched terminal helpers
(`_terminal_size`, `_terminal_columns`, `_clock_ns`) so the refresh thread is
not part of the assertions.
"""

import io
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import TextIO
from uuid import UUID, uuid4

from stario.telemetry.noop import NoOpTracer
from stario.telemetry.spans import RecordedEvent, RecordedLink, RecordingSpan
from stario.telemetry.tty import (
    TTYRenderer,
    TTYTracer,
    _clip_visible,
    _cut,
    _fmt_bytes,
    _fmt_duration,
    _LiveRegion,
    _visible_width,
)

_NOOP = NoOpTracer()

# Fixed wall-clock anchor so rendered times are stable: 2023-11-14T22:13:20Z.
_T0 = 1_700_000_000 * 1_000_000_000
_NOW = _T0 + 10_000_000
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FORBIDDEN_SGR = re.compile(r"\x1b\[(?:[34]0|37|9\d|4\d|10\d|38;)")
_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_DIM_RUN = re.compile(r"\x1b\[2m(.*?)\x1b\[0m")
_STAMP = re.compile(r"\d{2}:\d{2}:\d{2}")


def make_span(
    name: str = "request",
    *,
    parent: RecordingSpan | None = None,
    start_ns: int = _T0,
    end_ns: int | None = _T0 + 5_000_000,
    error: str | None = None,
    attributes: dict | None = None,
    events: list[RecordedEvent] | None = None,
    links: list[RecordedLink] | None = None,
) -> RecordingSpan:
    span_id = uuid4()
    return RecordingSpan(
        id=span_id,
        tracer=_NOOP,
        trace_id=span_id if parent is None else parent.trace_id,
        parent_id=None if parent is None else parent.id,
        name=name,
        start_ns=start_ns,
        end_ns=end_ns,
        error=error,
        attributes=attributes,
        events=events,
        links=links,
    )


def render(
    span: RecordingSpan,
    *,
    children: dict[UUID, list[RecordingSpan]] | None = None,
    width: int = 120,
    now: int = _NOW,
    color: bool = False,
    ascii: bool = False,
) -> str:
    return TTYRenderer(
        width, children or {}, now_ns=now, color=color, ascii=ascii
    ).history_block(span)


def _complex_tree() -> tuple[RecordingSpan, dict[UUID, list[RecordingSpan]]]:
    root = make_span(
        "request",
        attributes={
            "request.method": "GET",
            "request.path": "/projects/42",
            "user.id": 17,
            "session": "a81f09c2",
            "response.status_code": 200,
        },
        end_ns=_T0 + 48_200_000,
    )
    db = make_span(
        "db.query",
        parent=root,
        start_ns=_T0 + 1_200_000,
        end_ns=_T0 + 13_200_000,
        attributes={
            "db.statement": "SELECT * FROM projects WHERE id = ?",
            "db.rows": 1,
        },
        events=[
            RecordedEvent(
                time_ns=_T0 + 9_800_000,
                name="cache.miss",
                attributes={"key": "project:42"},
            )
        ],
    )
    render_span = make_span(
        "render",
        parent=root,
        start_ns=_T0 + 14_000_000,
        end_ns=_T0 + 47_100_000,
        attributes={"template": "project.html"},
    )
    fetch = make_span(
        "fetch avatar",
        parent=render_span,
        start_ns=_T0 + 20_000_000,
        end_ns=_T0 + 44_500_000,
        attributes={"url": "https://cdn.example/a/17.png", "note": "東京"},
    )
    return root, {root.id: [db, render_span], render_span.id: [fetch]}


def _startup() -> RecordingSpan:
    return make_span(
        "server.startup",
        attributes={
            "server.host": "127.0.0.1",
            "server.port": 8080,
            "server.tls": False,
            "server.compression.zstd_level": 3,
            "server.compression.brotli_level": 4,
            "server.compression.gzip_level": 1,
            "server.limits.max_request_header_bytes": 65536,
            "server.limits.max_request_body_bytes": 10_485_760,
        },
    )


class TestSpanStates:
    def test_running_leaf_shows_spinner_glyph(self):
        span = make_span("request", end_ns=None)
        line = render(span, width=90)

        frame = (_NOW // 125_000_000) % 10
        assert _SPINNER[frame] in line

    def test_spinner_frame_depends_on_now(self):
        span = make_span("request", end_ns=None)
        a = render(span, width=90, now=_NOW)
        b = render(span, width=90, now=_NOW + 125_000_000)
        assert _SPINNER[0] in a
        assert _SPINNER[1] in b

    def test_open_parent_with_open_child_is_waiting(self):
        root = make_span("request", end_ns=None)
        child = make_span(
            "db.query", parent=root, start_ns=_T0 + 2_000_000, end_ns=None
        )
        text = render(root, children={root.id: [child]}, width=90)

        root_line, child_line = text.splitlines()
        assert "○" in root_line
        assert any(g in child_line for g in _SPINNER)

    def test_done_and_failed_glyphs(self):
        assert "●" in render(make_span("request"), width=90)
        assert "×" in render(make_span("request", error="boom"), width=90)

    def test_child_connector_carries_the_status(self):
        root = make_span("request", end_ns=_T0 + 9_000_000)
        done = make_span("a", parent=root, start_ns=_T0 + 1, end_ns=_T0 + 2)
        failed = make_span(
            "b", parent=root, start_ns=_T0 + 3, end_ns=_T0 + 4, error="x"
        )
        text = render(root, children={root.id: [done, failed]}, width=90)

        assert "├─ a" in text
        assert "└× b" in text

    def test_open_parent_duration_covers_child(self):
        root = make_span("request", end_ns=None)
        child = make_span(
            "db.query", parent=root, start_ns=_T0 + 2_000_000, end_ns=None
        )
        text = render(root, children={root.id: [child]}, width=90, now=_T0 + 10_000_000)

        lines = text.splitlines()
        assert lines[0].endswith("10ms")
        assert lines[1].endswith("8.0ms")

    def test_orphan_when_root_ends_before_child(self):
        root = make_span("request", end_ns=_T0 + 10_000_000)
        child = make_span(
            "db.query", parent=root, start_ns=_T0 + 2_000_000, end_ns=None
        )
        text = render(root, children={root.id: [child]}, width=90)

        assert "└! db.query" in text
        assert "still open when the parent ended" in text


class TestHeaders:
    def test_name_promotion_removes_promoted_keys(self):
        root = make_span(
            "request",
            attributes={
                "request.method": "GET",
                "request.path": "/projects/42",
                "response.status_code": 200,
                "user.id": 17,
            },
        )
        text = render(root, width=90)

        assert "GET /projects/42  200" in text
        assert "request.method" not in text
        assert "request.path" not in text
        assert "response.status_code" not in text
        assert "user.id=17" in text

    def test_route_hidden_only_when_it_repeats_the_path(self):
        same = make_span(
            "request",
            attributes={
                "request.method": "GET",
                "request.path": "/",
                "http.route": "/",
            },
        )
        assert "http.route" not in render(same, width=90)
        other = make_span(
            "request",
            attributes={
                "request.method": "GET",
                "request.path": "/notes/4",
                "http.route": "/notes/{id}",
            },
        )
        assert "http.route=/notes/{id}" in render(other, width=90)

    def test_one_right_column_holds_durations(self):
        root, children = _complex_tree()
        text = render(root, children=children, width=90)

        # Four span durations and one event offset (+9.8ms) end at the edge.
        timed = [ln for ln in text.splitlines() if re.search(r"\d(ms|s|µs)$", ln)]
        assert len(timed) == 5
        for line in timed:
            assert _visible_width(line) == 90
        # Neither the start offsets nor the trace id tail are shown any more.
        assert "+1.2" not in text
        assert str(root.id)[-8:] not in text

    def test_line_width_caps_at_100(self):
        root, children = _complex_tree()
        for line in render(root, children=children, width=200).splitlines():
            assert _visible_width(line) <= 100

    def test_hot_child_duration_is_bold(self):
        root, children = _complex_tree()
        text = render(root, children=children, width=90, color=True)
        render_line = next(ln for ln in text.splitlines() if "render" in ln)
        db_line = next(ln for ln in text.splitlines() if "db.query" in ln)
        # render took 33 of 48 ms; db.query 12 ms.
        assert "\x1b[1m" in render_line
        assert "\x1b[1m" not in db_line

    def test_bare_root_is_one_line(self):
        span = make_span("request")
        assert "\n" not in render(span, width=90)
        assert "\n" not in render(span, width=40)


class TestHistoryStamp:
    def test_time_prefix_and_body_indent_at_wide_widths(self):
        root, children = _complex_tree()
        lines = render(root, children=children, width=90).splitlines()

        assert re.match(r"^\d{2}:\d{2}:\d{2} ● ", lines[0])
        for line in lines[1:]:
            assert line.startswith(" " * 9)

    def test_no_time_below_50(self):
        root = make_span(
            "request",
            attributes={"request.method": "GET", "request.path": "/x", "user.id": 3},
        )
        text = render(root, width=49)
        assert text.startswith("● GET /x")
        assert not _STAMP.search(text)


class TestAttributes:
    def test_pairs_are_key_equals_value(self):
        root = make_span("request", attributes={"aa": "1", "bb": "2", "cc": "3"})
        assert "aa=1 bb=2 cc=3" in render(root, width=90)

    def test_values_read_like_logfmt(self):
        root = make_span(
            "job",
            attributes={
                "done": True,
                "failed": False,
                "owner": None,
                "title": 'say "hi" now',
                "empty": "",
                "retained_bytes": 1_305_229,
                "body_bytes": 512,
                "count": 65536,
            },
        )
        text = render(root, width=120)
        assert "done=true failed=false owner=null" in text
        assert 'title="say \\"hi\\" now"' in text
        assert 'empty=""' in text
        assert "retained_bytes=1.2MiB" in text
        assert "body_bytes=512B count=65536" in text

    def test_home_directory_becomes_tilde(self):
        home = str(Path.home())
        root = make_span("job", attributes={"dir": f"{home}/data", "other": "/srv/x"})
        text = render(root, width=120)
        assert "dir=~/data" in text
        assert "other=/srv/x" in text

    def test_dotted_keys_group_under_a_label(self):
        text = render(_startup(), width=100)
        lines = text.splitlines()

        assert "host=127.0.0.1 port=8080 tls=false" in lines[1]
        compression = next(ln for ln in lines if "compression" in ln)
        assert compression.endswith(
            "compression  zstd_level=3 brotli_level=4 gzip_level=1"
        )
        limits = next(ln for ln in lines if "limits" in ln)
        assert (
            "limits       max_request_header_bytes=64KiB max_request_body_bytes=10MiB"
            in limits
        )

    def test_narrow_width_stacks_the_label(self):
        lines = render(_startup(), width=44).splitlines()
        i = next(i for i, ln in enumerate(lines) if ln.endswith("limits"))
        assert lines[i + 1].endswith("  max_request_header_bytes=64KiB")
        assert lines[i + 2].endswith("  max_request_body_bytes=10MiB")

    def test_single_key_prefix_stays_flat(self):
        root = make_span(
            "server.startup",
            attributes={
                "server.host": "0.0.0.0",
                "server.port": 8000,
                "limits.max_request_header_bytes": 65536,
            },
        )
        text = render(root, width=90)
        assert "host=0.0.0.0 port=8000" in text
        assert "limits.max_request_header_bytes=64KiB" in text

    def test_insertion_order_is_kept(self):
        root = make_span("job", attributes={"zeta": 1, "alpha": 2})
        assert "zeta=1 alpha=2" in render(root, width=90)

    def test_long_value_wraps_without_losing_info(self):
        value = " ".join(f"word{i:02d}" for i in range(40))
        root = make_span("request", attributes={"payload": value})
        text = render(root, width=50)

        assert 'payload="word00' in text
        assert "more lines" not in text
        joined = " ".join(text.split())
        for i in range(40):
            assert f"word{i:02d}" in joined

    def test_long_path_breaks_after_a_slash(self):
        root = make_span(
            "job", attributes={"dir": "/srv/yard/apps/querymarks/data/uploads/2026"}
        )
        lines = render(root, width=40).splitlines()
        assert lines[1].rstrip().endswith("/")

    def test_very_long_value_caps_at_twelve_lines(self):
        value = " ".join(f"word{i:02d}" for i in range(400))
        root = make_span("request", attributes={"payload": value})
        assert "more lines" in render(root, width=40)

    def test_multiline_value_becomes_block(self):
        root = make_span("request", attributes={"payload": "first\nsecond\nthird"})
        lines = render(root, width=90).splitlines()
        assert lines[1].endswith("payload=")
        assert lines[2].endswith("  first")
        assert lines[4].endswith("  third")


class TestEventsErrorsLinks:
    def test_event_line_and_body(self):
        root = make_span(
            "request",
            events=[
                RecordedEvent(
                    time_ns=_T0 + 1_000_000,
                    name="cache.miss",
                    attributes={"key": "p:42"},
                    body="lookup failed\ndetail",
                )
            ],
        )
        text = render(root, width=90)
        event_line = text.splitlines()[1]
        assert "└◦ cache.miss  key=p:42" in event_line
        assert event_line.endswith("+1.0ms")
        assert "lookup failed" in text

    def test_failed_span_shows_exc_type_and_frame(self):
        root = make_span(
            "request",
            error="bad password hash",
            events=[
                RecordedEvent(
                    time_ns=_T0 + 3_000_000,
                    name="exception",
                    attributes={
                        "exc.type": "ValueError",
                        "exc.message": "bad password hash",
                    },
                    body='Traceback (most recent call last):\n  File "a.py", line 1\nValueError: bad password hash',
                )
            ],
        )
        text = render(root, width=90)

        assert "×" in text
        assert "ValueError: bad password hash" in text
        assert "┌ Traceback" in text
        assert "└ ValueError" in text
        assert "◦ exception" not in text

    def test_frames_and_bodies_wrap_instead_of_clipping(self):
        body = "Traceback (most recent call last):\n" + "x" * 200 + "\nValueError: boom"
        root = make_span(
            "worker",
            error="boom",
            events=[
                RecordedEvent(
                    time_ns=_T0 + 1_000,
                    name="exception",
                    attributes={"exc.type": "ValueError", "exc.message": "boom"},
                    body=body,
                ),
                RecordedEvent(time_ns=_T0 + 2_000, name="note", body="y" * 200),
            ],
        )
        text = render(root, width=60)
        assert "…" not in text
        for line in text.splitlines():
            assert _visible_width(line) <= 60
        # Every character of both long rows survived the wrap.
        assert text.count("x") == 200
        assert text.count("y") == 200

    def test_exception_event_on_unfailed_span_shows_line_and_frame(self):
        root = make_span(
            "request",
            events=[
                RecordedEvent(
                    time_ns=_T0 + 1_000_000,
                    name="exception",
                    attributes={"exc.type": "ValueError", "exc.message": "hmm"},
                    body="Traceback:\nline",
                )
            ],
        )
        text = render(root, width=90)
        assert "◦ exception  ValueError: hmm" in text
        assert "┌ Traceback:" in text

    def test_links_render_arrow_lines(self):
        root = make_span(
            "request",
            links=[RecordedLink("parent.trace", uuid4(), {"kind": "retry"})],
        )
        text = render(root, width=90)
        assert "→ parent.trace" in text
        assert "kind=retry" in text

    def test_nested_collapse_marker_over_60_items(self):
        root = make_span(
            "request",
            events=[
                RecordedEvent(time_ns=_T0 + i * 1_000, name=f"e{i}") for i in range(70)
            ],
        )
        text = render(root, width=90)
        assert "… 10 more (0 spans, 10 events)" in text

    def test_sanitizing_blocks_escapes_and_controls(self):
        root = make_span(
            "req\nuest",
            attributes={"payload": "a\x1b[31mb\tc", "multi": "x\ny"},
        )
        text = render(root, width=90)
        assert "\x1b" not in text
        assert "\\x1b" in text
        assert "↵" in text
        for line in text.splitlines():
            assert _visible_width(line) <= 90


class TestColorRules:
    def _samples(self, width: int = 90) -> list[str]:
        root, children = _complex_tree()
        open_root = make_span(
            "request",
            end_ns=None,
            attributes={"request.method": "GET", "request.path": "/x"},
        )
        failed = make_span(
            "request",
            error="boom",
            events=[
                RecordedEvent(
                    time_ns=_T0 + 1,
                    name="exception",
                    attributes={"exc.type": "ValueError", "exc.message": "boom"},
                    body='Traceback (most recent call last):\n  File "a.py"',
                )
            ],
        )
        return [
            render(root, children=children, width=width, color=True),
            render(failed, width=width, color=True),
            TTYRenderer(width, children, now_ns=_NOW, color=True).live_text(
                [open_root], max_rows=12
            ),
        ]

    def test_no_forbidden_sgr_and_styles_closed(self):
        for text in self._samples():
            assert not _FORBIDDEN_SGR.search(text)
            for line in text.splitlines():
                assert _visible_width(line) <= 90
                open_style = False
                for match in _SGR.finditer(line):
                    open_style = match.group(1) != "0"
                assert not open_style, line

    def test_dim_never_hides_names_values_durations_or_tracebacks(self):
        dim = " ".join(m for text in self._samples() for m in _DIM_RUN.findall(text))
        for essential in (
            "GET",
            "db.query",
            "render",
            "a81f09c2",
            "project.html",
            "48ms",
            "12ms",
            "33ms",
            "Traceback",
            "a.py",
        ):
            assert essential not in dim, essential
        # Structure is dim: keys, rails, and the time prefix.
        assert "session=" in dim
        assert "├" in dim
        assert _STAMP.search(dim)

    def test_yellow_status_is_bolded(self):
        root = make_span("request", attributes={"response.status_code": 302})
        text = render(root, width=90, color=True)
        assert "\x1b[33m\x1b[1m302\x1b[0m" in text
        assert "\x1b[33m\x1b[1m●" in text


class TestAsciiGlyphs:
    def test_ascii_set_has_no_non_ascii_output(self):
        root, children = _complex_tree()
        failed = make_span(
            "request",
            error="boom",
            events=[
                RecordedEvent(
                    time_ns=_T0 + 1,
                    name="exception",
                    attributes={"exc.type": "ValueError", "exc.message": "boom"},
                    body="Traceback:\nline",
                )
            ],
            links=[RecordedLink("parent.trace", uuid4())],
        )
        root.attributes = {**(root.attributes or {}), "note": "東京", "multi": "a\nb"}
        texts = [
            render(root, children=children, width=90, ascii=True),
            render(failed, width=90, ascii=True),
            TTYRenderer(90, children, now_ns=_NOW, color=False, ascii=True).live_text(
                [make_span("request", end_ns=None)], max_rows=4
            ),
        ]
        for text in texts:
            # User text (東京) passes through; every glyph Stario adds is ASCII.
            assert text.replace("東京", "").isascii(), text

        tree = texts[0]
        assert "* GET /projects/42  200" in tree
        assert "|- db.query" in tree
        assert "`- render" in tree
        assert "+ cache.miss" in tree
        assert "x GET" not in tree
        assert "x request" in texts[1]
        assert "-> parent.trace" in texts[1]

    def test_ascii_ellipsis_and_micro(self):
        assert _fmt_duration(400_000, "us") == "400us"
        assert _clip_visible("abcdefghij", 6, "...") == "abc..."


class TestWidthSweep:
    def test_every_line_fits_every_width(self):
        root, children = _complex_tree()
        open_root, open_children = _complex_tree()
        open_root.end_ns = None
        startup = _startup()
        for width in range(1, 131):
            for color in (False, True):
                for ascii in (False, True):
                    texts = [
                        TTYRenderer(
                            width, children, now_ns=_NOW, color=color, ascii=ascii
                        ).history_block(root),
                        TTYRenderer(
                            width, {}, now_ns=_NOW, color=color, ascii=ascii
                        ).history_block(startup),
                        TTYRenderer(
                            width, open_children, now_ns=_NOW, color=color, ascii=ascii
                        ).live_text([open_root], max_rows=12),
                    ]
                    for text in texts:
                        for line in text.splitlines():
                            assert _visible_width(line) <= width
                            assert not line.endswith(" "), repr(line)


class TestLiveArea:
    def _open_tree(self):
        root = make_span(
            "request",
            end_ns=None,
            attributes={"request.method": "GET", "request.path": "/x"},
        )
        done = make_span(
            "db.query", parent=root, start_ns=_T0 + 1_000_000, end_ns=_T0 + 4_000_000
        )
        failed = make_span(
            "cache.put",
            parent=root,
            start_ns=_T0 + 4_000_000,
            end_ns=_T0 + 6_000_000,
            error="nope",
        )
        open_child = make_span(
            "render", parent=root, start_ns=_T0 + 6_000_000, end_ns=None
        )
        root.events = [RecordedEvent(time_ns=_T0 + 2_000_000, name="ev")]
        kids = {root.id: [done, failed, open_child]}
        return root, kids

    def test_only_active_path_shown_with_meta(self):
        root, kids = self._open_tree()
        text = TTYRenderer(90, kids, now_ns=_NOW, color=False).live_text(
            [root], max_rows=12
        )
        assert "render" in text
        assert "db.query" not in text
        assert "cache.put" not in text
        assert "1 done" in text
        assert "1 failed" in text
        assert "events" in text

    def test_max_rows_respected(self):
        root, kids = self._open_tree()
        for rows in (1, 2, 5, 12):
            text = TTYRenderer(90, kids, now_ns=_NOW, color=False).live_text(
                [root], max_rows=rows
            )
            assert len(text.splitlines()) <= rows

    def test_oldest_roots_dropped_first(self):
        roots = [
            make_span(f"r{i}", start_ns=_T0 + i * 1_000_000, end_ns=None)
            for i in range(4)
        ]
        text = TTYRenderer(90, {}, now_ns=_NOW, color=False).live_text(
            roots, max_rows=4
        )
        assert "── 4 open · 1 not shown" in text
        assert "r3" in text
        assert "r0" not in text

    def test_budget_one_shows_newest_header_only(self):
        roots = [
            make_span(f"r{i}", start_ns=_T0 + i * 1_000_000, end_ns=None)
            for i in range(3)
        ]
        text = TTYRenderer(90, {}, now_ns=_NOW, color=False).live_text(
            roots, max_rows=1
        )
        lines = text.splitlines()
        assert len(lines) == 1
        assert "r2" in lines[0]
        assert "open" not in lines[0]

    def test_root_glyph_is_waiting_while_a_child_is_open(self):
        root, kids = self._open_tree()
        text = TTYRenderer(90, kids, now_ns=_NOW, color=False).live_text(
            [root], max_rows=12
        )
        root_line = next(ln for ln in text.splitlines() if "GET /x" in ln)
        leaf_line = next(ln for ln in text.splitlines() if "render" in ln)
        assert root_line.startswith("○ GET /x")
        assert any(f"└{g} render" in leaf_line for g in _SPINNER)

        solo = make_span("alone", end_ns=None)
        alone = TTYRenderer(90, {}, now_ns=_NOW, color=False).live_text(
            [solo], max_rows=12
        )
        assert any(g in alone for g in _SPINNER)
        assert "○" not in alone

    def test_meta_never_clips_mid_token(self):
        root, kids = self._open_tree()
        meta = " · 1 done · 1 failed · 1 events"
        for width in range(20, 60):
            text = TTYRenderer(width, kids, now_ns=_NOW, color=False).live_text(
                [root], max_rows=12
            )
            root_line = text.splitlines()[1]
            # Meta is appended whole or dropped entirely.
            assert ("·" in root_line) == (meta in root_line), root_line

    def test_running_leaf_attributes_and_root_attributes_last(self):
        root = make_span("request", end_ns=None, attributes={"root.attr": 1})
        child = make_span(
            "db.query",
            parent=root,
            start_ns=_T0 + 1_000_000,
            end_ns=None,
            attributes={"sql.rows": 5},
        )
        kids = {root.id: [child]}
        text = TTYRenderer(90, kids, now_ns=_NOW, color=False).live_text(
            [root], max_rows=12
        )
        assert "sql.rows=5" in text
        assert "root.attr=1" in text
        # With a tight budget the running leaf keeps its attrs before the root's.
        tight = TTYRenderer(90, kids, now_ns=_NOW, color=False).live_text(
            [root], max_rows=4
        )
        assert "sql.rows" in tight
        assert "root.attr" not in tight

    def test_live_attributes_clip_to_one_row(self):
        root = make_span("request", end_ns=None, attributes={"payload": "x" * 300})
        text = TTYRenderer(60, {}, now_ns=_NOW, color=False).live_text(
            [root], max_rows=12
        )
        lines = text.splitlines()
        assert len(lines) == 3
        assert lines[2].startswith("  payload=xxx")
        assert lines[2].endswith("…")


class _CountingIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    def write(self, s: str) -> int:
        self.writes += 1
        return super().write(s)


class _TermPatch:
    def __init__(self, columns: int, lines: int, now: int) -> None:
        self.columns = columns
        self.lines = lines
        self.now = now

    def size(self) -> tuple[int, int]:
        return max(1, self.columns - 1), max(1, self.lines)

    def cols(self) -> int:
        return self.columns

    def clock(self) -> int:
        return self.now


def _patch_terminal(monkeypatch, *, columns=80, lines=24, now=_NOW) -> _TermPatch:
    import stario.telemetry.tty as tty_module

    term = _TermPatch(columns, lines, now)
    monkeypatch.setattr(tty_module, "_terminal_size", term.size)
    monkeypatch.setattr(tty_module, "_terminal_columns", term.cols)
    monkeypatch.setattr(tty_module, "_clock_ns", term.clock)
    return term


def _manual_tracer(out: TextIO, *, live=True) -> TTYTracer:
    tracer = TTYTracer(out=out, live=live)
    tracer._running = True
    return tracer


def _open_root(tracer: TTYTracer, name="request") -> RecordingSpan:
    root = tracer.create(name)
    root.start()
    root.start_ns = _T0
    return root


class TestTracerIO:
    def test_unchanged_live_frame_writes_nothing(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = _CountingIO()
        tracer = _manual_tracer(out)
        _open_root(tracer)

        tracer._render()
        first = out.writes

        tracer._render()
        assert out.writes == first

    def test_frame_is_one_write_wrapped_in_sync_markers(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = _CountingIO()
        tracer = _manual_tracer(out)
        _open_root(tracer)

        tracer._render()
        text = out.getvalue()
        assert out.writes == 1
        assert text.startswith("\x1b[?2026h")
        assert text.endswith("\x1b[?2026l")

    def test_history_and_live_still_one_write(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = _CountingIO()
        tracer = _manual_tracer(out)
        done = _open_root(tracer, "done.request")
        done.end()
        _open_root(tracer, "keep.request")

        tracer._render()
        text = out.getvalue()
        assert out.writes == 1
        assert "done.request" in text
        assert "keep.request" in text

    def test_erase_reflow_aware_after_column_shrink(self, monkeypatch):
        term = _patch_terminal(monkeypatch, columns=80)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        root = _open_root(tracer)
        root.attr("pad", "x" * 120)
        root.attr("pad2", "y" * 120)

        tracer._render()
        live_lines = tracer._region._line_widths
        assert live_lines
        assert all(w == 79 for w in live_lines)

        term.columns = 40
        root.attr("pad3", "z" * 5)
        tracer._render()

        expected_rows = sum(-(-w // 40) for w in live_lines)
        assert f"\x1b[{expected_rows}A" in out.getvalue()
        assert expected_rows == 2 * len(live_lines)

    def test_erase_starts_with_cr_and_clears_down(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        root = _open_root(tracer)
        tracer._render()

        root.attr("k", "v")
        tracer._render()
        assert "\r" in out.getvalue()
        assert "\x1b[J" in out.getvalue()

    def test_live_false_never_writes_escapes(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out, live=False)
        root = _open_root(tracer)
        tracer._render()
        root.end()
        tracer._render()

        text = out.getvalue()
        assert "request" in text
        assert "\x1b[" not in text

    def test_root_end_formats_lazily_on_render_thread(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        calls = 0
        original = TTYRenderer.history_block

        def counting(self, root):
            nonlocal calls
            calls += 1
            return original(self, root)

        monkeypatch.setattr(TTYRenderer, "history_block", counting)
        root = _open_root(tracer)
        root.end()
        assert calls == 0

        tracer._render()
        assert calls == 1
        assert "request" in out.getvalue()

    def test_flush_is_safe_to_call_twice(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        root = _open_root(tracer)
        root.end()
        tracer.flush()
        tracer.flush()
        assert "request" in out.getvalue()

    def test_non_utf_stream_switches_to_ascii_glyphs(self, monkeypatch):
        _patch_terminal(monkeypatch)
        buf = io.BytesIO()
        out = io.TextIOWrapper(buf, encoding="ascii")
        tracer = _manual_tracer(out, live=False)
        assert tracer._ascii
        root = _open_root(tracer)
        root.attr("note", "東京")
        root.end()

        tracer._render()  # must not raise UnicodeEncodeError
        out.flush()
        data = buf.getvalue().decode("ascii")
        assert "* request" in data  # the ASCII glyph, not a lossy "?"
        assert "note=??" in data  # user text still lossy-encodes

    def test_ascii_can_be_forced_on_a_utf_stream(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = TTYTracer(out=out, live=False, ascii=True)
        tracer._running = True
        root = _open_root(tracer)
        root.end()
        tracer._render()
        assert out.getvalue().split(" ", 1)[1].startswith("* request")

    def test_dumb_terminal_gets_plain_history(self, monkeypatch):
        _patch_terminal(monkeypatch)
        monkeypatch.setenv("TERM", "dumb")
        monkeypatch.delenv("NO_COLOR", raising=False)
        out = _FakeTTY()
        tracer = TTYTracer(out=out)
        tracer._running = True
        assert not tracer._live_enabled
        assert not tracer._color
        root = _open_root(tracer)
        tracer._render()
        root.end()
        tracer._render()
        assert "request" in out.getvalue()
        assert "\x1b" not in out.getvalue()

    def test_blank_line_around_multiline_blocks_only(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out, live=False)
        for name in ("one", "two"):
            _open_root(tracer, name).end()
        tree = _open_root(tracer, "tree")
        tree.attr("k", "v")
        tree.end()
        _open_root(tracer, "three").end()
        tracer._render()

        lines = out.getvalue().splitlines()
        assert len(lines) == 7
        assert "one" in lines[0]
        assert "two" in lines[1]
        assert lines[2] == ""
        assert "tree" in lines[3]
        assert lines[4].strip() == "k=v"
        assert lines[5] == ""
        assert "three" in lines[6]

    def test_history_render_failure_emits_fallback(self, monkeypatch):
        class _BadStr:
            def __str__(self) -> str:
                raise RuntimeError("unprintable")

        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out, live=False)
        root = _open_root(tracer, "broken.request")
        root.attr("bad", _BadStr())
        root.end()

        tracer._render()
        text = out.getvalue()
        assert "broken.request" in text
        assert "render failed: RuntimeError" in text

    def test_live_render_failure_emits_fallback_line(self, monkeypatch):
        class _BadStr:
            def __str__(self) -> str:
                raise RuntimeError("unprintable")

        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        root = _open_root(tracer, "open.request")
        root.attr("bad", _BadStr())

        tracer._render()
        assert "live view failed: RuntimeError" in out.getvalue()

    def test_context_manager_flushes_open_roots(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        with TTYTracer(out=out, live=True) as tracer:
            root = tracer.create("request.in-flight", {"http.method": "GET"})
            root.start()
            child = tracer.create("db.query", parent=root)
            child.start()
            child.end()

        text = out.getvalue()
        assert "request.in-flight" in text
        assert "db.query" in text
        assert "http.method" in text


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestPassthrough:
    def _proxy_tracer(self, monkeypatch) -> tuple[TTYTracer, _FakeTTY]:
        _patch_terminal(monkeypatch)
        fake = _FakeTTY()
        monkeypatch.setattr(sys, "stdout", fake)
        tracer = TTYTracer()
        tracer._running = True
        tracer._install_passthrough()
        return tracer, fake

    def test_print_yields_live_block_and_repaints(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        try:
            _open_root(tracer)
            tracer._render()
            assert tracer._region.has_content

            print("hello")
            text = fake.getvalue()
            assert "\x1b[J" in text
            assert text.index("\x1b[J") < text.index("hello\n")

            tracer._render()
            assert tracer._region.has_content
            text = tracer._region._text
            assert text is not None
            assert "request" in text
        finally:
            tracer._restore_streams()

    def test_partial_write_pauses_and_newline_resumes(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        try:
            root = _open_root(tracer)
            tracer._render()

            sys.stdout.write("prompt> ")
            sys.stdout.flush()
            assert tracer._paused
            fake.seek(0)
            fake.truncate()
            tracer._render()
            assert fake.getvalue() == ""

            sys.stdout.write("answer\n")
            assert not tracer._paused
            tracer._render()
            assert "request" in fake.getvalue()
            root.end()
        finally:
            tracer._restore_streams()

    def test_streams_restored(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        assert isinstance(sys.stdout, type(tracer._out_proxy))
        tracer._restore_streams()
        assert sys.stdout is fake
        assert sys.stderr is not tracer._err_proxy

    def test_stderr_not_proxied_when_not_a_tty(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out_tty = _FakeTTY()
        err_file = io.StringIO()  # redirected stderr: isatty() is False
        monkeypatch.setattr(sys, "stdout", out_tty)
        monkeypatch.setattr(sys, "stderr", err_file)

        tracer = TTYTracer()
        tracer._running = True
        tracer._install_passthrough()
        try:
            assert sys.stderr is err_file
            _open_root(tracer)
            tracer._render()
            print("oops", file=sys.stderr)
            assert "oops" in err_file.getvalue()
            assert "\x1b[" not in err_file.getvalue()
        finally:
            tracer._restore_streams()

    def test_stderr_tty_text_erases_via_stdout(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out_tty = _FakeTTY()
        err_tty = _FakeTTY()
        monkeypatch.setattr(sys, "stdout", out_tty)
        monkeypatch.setattr(sys, "stderr", err_tty)

        tracer = TTYTracer()
        tracer._running = True
        tracer._install_passthrough()
        try:
            assert sys.stderr is not err_tty
            _open_root(tracer)
            tracer._render()
            print("oops", file=sys.stderr)
            # The erase went to the tracer's out; the text to stderr.
            assert "\x1b[J" in out_tty.getvalue()
            assert "oops" in err_tty.getvalue()
            assert "\x1b[" not in err_tty.getvalue()
        finally:
            tracer._restore_streams()

    def test_writelines_goes_through_the_proxy_buffer(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        try:
            _open_root(tracer)
            tracer._render()
            fake.seek(0)
            fake.truncate()

            sys.stdout.writelines(["one\n", "two\n"])
            assert "one\ntwo\n" in fake.getvalue()
            assert "\x1b[J" in fake.getvalue()
        finally:
            tracer._restore_streams()

    def test_resume_frame_starts_on_a_fresh_line(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        try:
            _open_root(tracer)
            tracer._render()
            sys.stdout.write("prompt> ")
            sys.stdout.flush()
            assert tracer._paused

            fake.seek(0)
            fake.truncate()
            span = tracer.create("after.pause")
            span.start()
            span.end()
            tracer._render()
            # The newline keeps live/history off the prompt line.
            assert fake.getvalue().startswith("\x1b[?2026h\n")
        finally:
            tracer._restore_streams()

    def test_span_activity_resumes_paused_rendering(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        try:
            _open_root(tracer)
            tracer._render()
            # Partial line + flush: pauses (pdb prompt scenario).
            sys.stdout.write("prompt> ")
            sys.stdout.flush()
            assert tracer._paused

            fake.seek(0)
            fake.truncate()
            tracer._render()
            assert fake.getvalue() == ""

            # Any span activity resumes; a root end queues history.
            span = tracer.create("after.pause")
            assert not tracer._paused
            span.start()
            span.end()
            tracer._render()
            assert "after.pause" in fake.getvalue()
        finally:
            tracer._restore_streams()

    def test_logging_handler_retargeted_and_restored(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        logger = logging.getLogger("stario.test.tty.passthrough")
        handler = logging.StreamHandler(fake)
        logger.addHandler(handler)
        try:
            tracer._restore_streams()
            tracer._install_passthrough()
            assert handler.stream is tracer._out_proxy
            tracer._restore_streams()
            assert handler.stream is fake
        finally:
            tracer._restore_streams()
            logger.removeHandler(handler)


class TestThreadSafety:
    def test_concurrent_mutation_and_render(self, monkeypatch):
        # Each thread does a fixed amount of work. With open-ended producers
        # the renderer could be starved of the GIL for a while, the backlog
        # of finished traces grew into the tens of thousands, and the first
        # render then formatted all of it under the lock: seconds locally,
        # past the test timeout on a slow runner.
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        errors: list[BaseException] = []

        def hammer(i: int) -> None:
            try:
                for _ in range(200):
                    root = tracer.create(f"worker.{i}")
                    root.start()
                    root.attr("k", "v")
                    root.event("tick", {"i": i})
                    child = root.step("db.query")
                    child.start()
                    child.end()
                    root.end()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            tracer._render()
        for t in threads:
            t.join()
        tracer._render()

        assert not errors
        rendered = out.getvalue()
        assert all(f"worker.{i}" in rendered for i in range(4))


class TestShutdown:
    def test_stop_gives_up_on_a_write_lock_held_by_a_stuck_write(self, monkeypatch):
        import stario.telemetry.tty as tty_module

        _patch_terminal(monkeypatch)
        monkeypatch.setattr(tty_module, "_STOP_WAIT_S", 0.05)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        _open_root(tracer).end()
        proxy = tty_module._Passthrough(
            out, tracer._write_lock, lambda: None, lambda _paused: None
        )
        proxy.write("partial line")
        tracer._out_proxy = proxy

        tracer._write_lock.acquire()  # the render thread, stuck in a write
        stopper = threading.Thread(target=tracer._stop, daemon=True)
        stopper.start()
        stopper.join(2.0)
        finished = not stopper.is_alive()
        tracer._write_lock.release()

        assert finished
        assert out.getvalue() == ""


class TestPerformance:
    def test_live_frame_with_many_roots_is_fast(self):
        children: dict[UUID, list[RecordingSpan]] = {}
        roots = []
        for i in range(50):
            root = make_span(f"request.{i}", start_ns=_T0 - i * 1_000_000, end_ns=None)
            kids = [
                make_span(
                    f"child.{i}.{j}",
                    parent=root,
                    start_ns=_T0 + j * 100,
                    end_ns=None,
                )
                for j in range(20)
            ]
            children[root.id] = kids
            roots.append(root)

        renderer = TTYRenderer(120, children, now_ns=_NOW, color=False)
        start = time.perf_counter()
        renderer.live_text(roots, max_rows=24)
        assert time.perf_counter() - start < 2.0

    def test_history_block_with_two_thousand_items_is_fast(self):
        root = make_span("request", end_ns=_T0 + 100_000_000)
        kids = [
            make_span(f"child.{i}", parent=root, start_ns=_T0 + i, end_ns=_T0 + i + 1)
            for i in range(1000)
        ]
        root.events = [
            RecordedEvent(time_ns=_T0 + i, name=f"e{i}") for i in range(1000)
        ]
        renderer = TTYRenderer(120, {root.id: kids}, now_ns=_NOW, color=False)
        start = time.perf_counter()
        renderer.history_block(root)
        assert time.perf_counter() - start < 2.0


class TestLiveRegion:
    def test_erase_counts_rows_from_stored_widths(self):
        region = _LiveRegion()
        region.store("a" * 70 + "\n" + "b" * 70 + "\n", 79)
        seq = region.erase_seq(80)
        assert seq == "\r\x1b[2A\x1b[J"
        assert not region.has_content

        region.store("c" * 70 + "\n" + "d" * 70 + "\n", 79)
        seq = region.erase_seq(40)
        assert seq == "\r\x1b[4A\x1b[J"

        # An empty region never wipes the line the cursor is on.
        seq = region.erase_seq(40)
        assert seq == ""

    def test_matches_requires_same_text_and_width(self):
        region = _LiveRegion()
        region.store("hello\n", 80)
        assert region.matches("hello\n", 80)
        assert not region.matches("hello\n", 40)
        assert not region.matches("other\n", 80)


class TestHelpers:
    def test_visible_width_fullwidth(self):
        assert _visible_width("中") == 2
        assert _visible_width("東京") == 4
        assert _visible_width("é") == 1

    def test_clip_respects_cell_budget(self):
        clipped = _clip_visible("東京" * 10, 7)
        assert _visible_width(clipped) <= 7
        assert clipped.endswith("…")

    def test_compact_durations(self):
        assert _fmt_duration(4_200) == "4.2µs"
        assert _fmt_duration(42_000) == "42µs"
        assert _fmt_duration(7_300_000) == "7.3ms"
        assert _fmt_duration(264_000_000) == "264ms"
        assert _fmt_duration(28_260_000_000) == "28.26s"
        assert _fmt_duration(65_000_000_000) == "1m05s"
        assert _fmt_duration(3_720_000_000_000) == "1h02m"

    def test_binary_byte_sizes(self):
        assert _fmt_bytes(512) == "512B"
        assert _fmt_bytes(65536) == "64KiB"
        assert _fmt_bytes(10_485_760) == "10MiB"
        assert _fmt_bytes(1_305_229) == "1.2MiB"

    def test_cut_prefers_space_then_slash(self):
        assert _cut("alpha beta gamma", 12) == ("alpha beta", "gamma")
        assert _cut("/srv/yard/apps/data", 12) == ("/srv/yard/", "apps/data")
        assert _cut("abcdefghij", 4) == ("abcd", "efghij")
