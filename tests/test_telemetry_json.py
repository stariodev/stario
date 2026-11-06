import io
import json
import threading
import time

import pytest

from stario.telemetry import TelemetryStats
from stario.telemetry.json import JsonTracer


class BrokenWriteOutput(io.StringIO):
    def write(self, s: str) -> int:
        raise OSError("closed collector")


def test_json_tracer_buffers_and_flushes_on_exit() -> None:
    output = io.StringIO()

    with JsonTracer(
        output=output,
        flush_interval=60.0,
        max_batch_spans=2,
    ) as tracer:
        with tracer.create("request") as span:
            span.attr("phase", "run")

        assert output.getvalue() == ""

    payload = json.loads(output.getvalue())
    assert payload["name"] == "request"
    assert payload["attributes"] == {"phase": "run"}


def test_json_tracer_writes_ndjson_file_and_reopens(tmp_path) -> None:
    path = tmp_path / "sub" / "spans.ndjson"
    tracer = JsonTracer(path=path, flush_interval=60.0)

    with tracer:
        with tracer.create("one"):
            pass
        with tracer.create("two"):
            pass

    assert tracer._output.closed
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["name"] for line in lines] == ["one", "two"]

    with tracer:
        with tracer.create("three"):
            pass
        with tracer.create("four"):
            pass

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["name"] for line in lines] == ["one", "two", "three", "four"]


def test_json_tracer_rejects_output_and_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="output or path, not both"):
        JsonTracer(output=io.StringIO(), path=tmp_path / "spans.ndjson")


def test_json_tracer_counts_background_write_errors(capsys) -> None:
    tracer = JsonTracer(
        output=BrokenWriteOutput(), flush_interval=60.0, max_batch_spans=1
    )

    with tracer, tracer.create("request"):
        pass

    assert "json tracer write error" in capsys.readouterr().err
    stats = tracer.stats()
    assert stats == TelemetryStats(
        writer_error_count=1,
        last_writer_error="OSError: closed collector",
        last_writer_error_at_ns=stats.last_writer_error_at_ns,
    )


def test_json_tracer_skips_non_serializable_span(capsys) -> None:
    output = io.StringIO()
    tracer = JsonTracer(
        output=output,
        flush_interval=60.0,
        max_batch_spans=10,
    )

    cyclical: dict[str, object] = {}
    cyclical["self"] = cyclical

    with tracer:
        with tracer.create("bad") as span:
            span.attr("key", cyclical)
        with tracer.create("good") as span:
            span.attr("ok", True)

    assert tracer.stats().serialization_error_count == 1
    assert "json tracer serialization error" in capsys.readouterr().err
    lines = [
        json.loads(line) for line in output.getvalue().splitlines() if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["name"] == "good"


class BlockingOutput(io.StringIO):
    """Blocks the first write until released, so the writer thread stays busy."""

    def __init__(self) -> None:
        super().__init__()
        self.blocked = threading.Event()
        self.release = threading.Event()

    def write(self, s: str) -> int:
        if not self.blocked.is_set():
            self.blocked.set()
            assert self.release.wait(5)
        return super().write(s)


def test_json_tracer_exports_message_less_exception_as_error() -> None:
    output = io.StringIO()

    with (
        pytest.raises(KeyError),
        JsonTracer(output=output, flush_interval=60.0) as tracer,
        tracer.create("request"),
    ):
        raise KeyError()

    payload = json.loads(output.getvalue())
    assert payload["status"] == "error"
    assert payload["error"] == "KeyError"
    assert payload["events"][0]["attributes"]["exc.type"] == "KeyError"


def test_json_tracer_reports_a_full_queue_once_a_second(capsys) -> None:
    output = BlockingOutput()
    tracer = JsonTracer(
        output=output, flush_interval=60.0, max_pending_spans=1, max_batch_spans=1
    )

    with tracer:
        with tracer.create("busy"):
            pass
        assert output.blocked.wait(5)  # the writer is inside a write
        with tracer.create("queued"):
            pass
        for _ in range(3):
            with tracer.create("dropped"):
                pass
        # A second later the next drop reports everything since the last one.
        assert tracer._drop_reported_at_ns is not None
        tracer._drop_reported_at_ns -= 1_000_000_000
        with tracer.create("dropped"):
            pass
        output.release.set()

    err = capsys.readouterr().err
    messages = [line for line in err.splitlines() if "pending queue full" in line]
    assert len(messages) == 2
    assert "dropped 1 finished span since the last report" in messages[0]
    assert "dropped 3 finished spans since the last report" in messages[1]
    assert tracer.stats().dropped_spans == 4
    names = [json.loads(line)["name"] for line in output.getvalue().splitlines()]
    assert names == ["busy", "queued"]


def test_json_tracer_exit_keeps_a_span_that_ends_while_the_writer_drains(
    capsys,
) -> None:
    output = BlockingOutput()
    tracer = JsonTracer(output=output, flush_interval=60.0, max_batch_spans=1)
    tracer.__enter__()
    with tracer.create("first"):
        pass
    assert output.blocked.wait(5)  # the writer is inside a write
    late = tracer.create("late")
    late.start()

    stopper = threading.Thread(target=tracer.__exit__, args=(None, None, None))
    stopper.start()
    while tracer._thread is not None:  # exit has asked the writer to stop
        time.sleep(0.001)
    late.end()
    output.release.set()
    stopper.join(5)
    assert not stopper.is_alive()

    names = [json.loads(line)["name"] for line in output.getvalue().splitlines()]
    assert names == ["first", "late"]
    assert tracer.stats().dropped_spans == 0
    assert "while stopped" not in capsys.readouterr().err
