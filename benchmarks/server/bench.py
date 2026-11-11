#!/usr/bin/env python3
"""HTTP benchmark runner: Stario against other Python frameworks.

Each target runs as one process pinned to one CPU (``SERVER_CPUS``); the
load generator is pinned to other CPUs (``CLIENT_CPUS``), so every target
gets the same core budget and none competes with ``wrk``. Before anything is
measured, every endpoint of every target is fetched and checked: status,
body, and the ``/page`` document must match Stario's (after entity and
doctype normalization).

Phases (``--phases``, comma-separated, default all):

- ``throughput`` — plaintext, json, params, validate, page at one
  concurrency; requests/s, p50 and p99 latency, server CPU.
- ``sweep`` — ``/page`` at several concurrencies (latency under load).
- ``mixed`` — a browser-shaped request mix with full browser headers.
- ``compress`` — restarts each target with its response compression on
  (where the framework has one) and measures ``/page`` and the mix with a
  browser's ``Accept-Encoding``; reports bytes on the wire.
- ``sse`` — N clients hold a Datastar SSE stream; the server broadcasts an
  HTML fragment every 0.5 s. Reports delivery latency, server CPU, RSS.
- ``conns`` — N keep-alive clients each send one request every 2 s
  (a page of "connected users"); reports RSS per connection and latency.

Standard library only; run with any Python 3.12+:

    python3 benchmarks/server/bench.py
    python3 benchmarks/server/bench.py --targets stario,fastapi --phases throughput
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import html
import json
import os
import re
import resource
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
ENVS = HERE / ".venvs"
RESULTS = HERE / "results"

TARGETS = [
    "stario",
    "fastapi",
    "blacksheep-uvicorn",
    "blacksheep-granian",
    "sanic",
    "falcon",
    "robyn",
    "stario-ft",  # free-threaded 3.14t build; used by the multicore phase
    "stario-mp",  # N GIL processes on SO_REUSEPORT (apps/stario_mp.py); multicore only
    # Stario on loopyloop (github.com/Bobowski/loopyloop), an asyncio loop on io_uring
    # with an epoll fallback. Opt-in: name them on the command line (LOOPYLOOP_SPEC).
    "stario-loopy",  # io_uring backend
    "stario-loopy-epoll",  # epoll backend
    "stario-mp-loopy",  # stario-mp on loopyloop (io_uring); multicore only
    "stario-ft-loopy",  # stario-ft on loopyloop (io_uring, PYTHON_GIL=0); multicore only
]
LOOPY = {"stario-loopy", "stario-loopy-epoll", "stario-mp-loopy", "stario-ft-loopy"}
# Targets whose framework ships response compression (enabled in `compress`).
COMPRESSING = {"stario", "stario-loopy", "stario-loopy-epoll", "fastapi", "blacksheep-uvicorn",
               "blacksheep-granian"}
NO_SSE: set[str] = set()

ENDPOINTS = {
    "plaintext": "/plaintext",
    "json": "/json",
    "params": "/user/42",
    "validate": "/validate",
    "page": "/page",
}

BROWSER_AE = "gzip, deflate, br, zstd"
LOOPY_SPEC = os.environ.get("LOOPYLOOP_SPEC", f"loopyloop @ file://{ROOT.parent / 'loopyloop'}")

PACKAGES = {
    "stario": [os.environ.get("STARIO_SPEC", f"stario @ file://{ROOT}"), "uvloop", "orjson"],
    "fastapi": ["fastapi", "uvicorn[standard]", "jinja2"],
    "blacksheep-uvicorn": ["blacksheep", "uvicorn[standard]", "orjson", "pydantic", "jinja2"],
    "blacksheep-granian": ["blacksheep", "granian[uvloop]", "orjson", "pydantic", "jinja2"],
    # uvloop + ujson come with sanic; no sanic-ext (installing it auto-loads CORS, OpenAPI, ...)
    "sanic": ["sanic", "orjson", "jinja2"],
    "falcon": ["falcon", "uvicorn[standard]", "msgspec", "jinja2"],
    "robyn": ["robyn[pydantic,templating]"],
    "stario-ft": [os.environ.get("STARIO_FT_SPEC", f"stario @ file://{ROOT}"), "uvloop", "msgspec"],
    "stario-loopy": [os.environ.get("STARIO_SPEC", f"stario @ file://{ROOT}"), "orjson", LOOPY_SPEC],
    "stario-ft-loopy": [os.environ.get("STARIO_FT_SPEC", f"stario @ file://{ROOT}"), "msgspec",
                        LOOPY_SPEC],
}
# Targets of the multicore phase (each gets MC_WORKERS workers / threads).
MULTICORE = ["stario-ft", "stario-mp", "fastapi", "blacksheep-granian", "sanic", "robyn",
             "stario-ft-loopy", "stario-mp-loopy"]
MULTICORE_ONLY = {"stario-ft", "stario-mp", "stario-ft-loopy", "stario-mp-loopy"}


def log(*args) -> None:
    print(*args, flush=True)


def env_name(target: str) -> str:
    # stario-mp reuses the GIL env; the loopyloop variants share one env per build.
    return {"stario-mp": "stario", "stario-loopy-epoll": "stario-loopy",
            "stario-mp-loopy": "stario-loopy"}.get(target, target)


def py(target: str) -> str:
    return str(ENVS / env_name(target) / "bin" / "python")


def ensure_env(target: str, python: str, refresh: bool) -> None:
    target = env_name(target)
    env_dir = ENVS / target
    if refresh and env_dir.exists():
        shutil.rmtree(env_dir)
    if not Path(py(target)).exists():
        if target in ("stario-ft", "stario-ft-loopy"):
            python += "t"
        subprocess.run(["uv", "venv", "-q", str(env_dir), "--python", python], check=True)
        subprocess.run(
            ["uv", "pip", "install", "-q", "--python", py(target), *PACKAGES[target]],
            check=True,
        )


def versions(target: str) -> str:
    names = {
        "stario": ["stario", "uvloop", "orjson"],
        "stario-ft": ["stario", "uvloop", "msgspec"],
        "stario-mp": ["stario", "uvloop", "orjson"],
        "stario-loopy": ["stario", "loopyloop", "orjson"],
        "stario-ft-loopy": ["stario", "loopyloop", "msgspec"],
        "fastapi": ["fastapi", "starlette", "uvicorn", "pydantic", "jinja2"],
        "blacksheep-uvicorn": ["blacksheep", "uvicorn", "orjson", "pydantic", "jinja2"],
        "blacksheep-granian": ["blacksheep", "granian", "orjson", "pydantic", "jinja2"],
        "sanic": ["sanic", "orjson", "jinja2"],
        "falcon": ["falcon", "uvicorn", "msgspec", "jinja2"],
        "robyn": ["robyn", "pydantic", "jinja2"],
    }[env_name(target)]
    code = (
        "import importlib.metadata as m, sys;"
        f"print(' '.join(f'{{n}}=={{m.version(n)}}' for n in {names!r}),"
        "'py' + '.'.join(map(str, sys.version_info[:3])))"
    )
    out = subprocess.run([py(target), "-c", code], capture_output=True, text=True)
    return out.stdout.strip() or out.stderr.strip()


# --- server process ---------------------------------------------------------


def server_cmd(target: str, host: str, port: int, workers: int = 1) -> list[str]:
    uvicorn = lambda app: [  # noqa: E731
        py(target), "-m", "uvicorn", app, "--host", host, "--port", str(port),
        "--workers", str(workers), "--loop", "uvloop", "--http", "httptools",
        "--no-access-log", "--log-level", "warning",
    ]
    match target:
        case "stario" | "stario-ft":
            # stario-ft: STARIO_THREADS (set in start_server) event-loop threads.
            return [
                py(target), "-c",
                "import uvloop; from stario import serve; "
                "from apps.stario_app import bootstrap; uvloop.run(serve(bootstrap))",
            ]
        case "stario-mp" | "stario-mp-loopy":
            # stario-mp-loopy: BENCH_LOOP=loopyloop (set in start_server)
            return [py(target), "-m", "apps.stario_mp", str(workers)]
        case "stario-loopy" | "stario-loopy-epoll" | "stario-ft-loopy":
            return [py(target), "-m", "apps.stario_loopy"]
        case "fastapi":
            return uvicorn("apps.fastapi_app:app")
        case "blacksheep-uvicorn":
            return uvicorn("apps.blacksheep_app:app")
        case "falcon":
            return uvicorn("apps.falcon_app:app")
        case "blacksheep-granian":
            return [
                py(target), "-m", "granian", "apps.blacksheep_app:app",
                "--interface", "asgi", "--host", host, "--port", str(port),
                "--workers", str(workers), "--runtime-mode", "st", "--runtime-threads", "1",
                "--loop", "uvloop", "--http", "1",
                # Granian's defaults (backlog 1024, backpressure = backlog / workers)
                # cap one worker at 1,024 concurrent connections; Stario and Uvicorn
                # listen with a 2048 backlog and no cap.
                "--backlog", "2048", "--backpressure", "65535",
                "--no-access-log", "--log-level", "warning",
            ]
        case "sanic":
            return [py(target), str(HERE / "apps/sanic_app.py"), "--host", host, "--port", str(port),
                    "--workers", str(workers)]
        case "robyn":
            return [
                py(target), str(HERE / "apps/robyn_app.py"), "--host", host, "--port", str(port),
                "--processes", str(workers), "--workers", "1", "--log-level", "WARN", "--disable-openapi",
            ]
    raise ValueError(target)


def parse_cpus(spec: str) -> set[int]:
    """'0', '2,3', '0-1,3' -> a set of CPU numbers."""
    out: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            out.update(range(int(lo), int(hi) + 1))
        elif part:
            out.add(int(part))
    return out


def cpu_times() -> dict[int, list[int]]:
    """Per-CPU jiffies from /proc/stat: user nice system idle iowait irq softirq steal."""
    out = {}
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("cpu") and line[3].isdigit():
            name, *vals = line.split()
            out[int(name[3:])] = [int(v) for v in vals[:8]]
    return out


def cpu_usage(t0: dict, t1: dict, cpus: set[int]) -> tuple[float, float]:
    """Mean (busy, steal) share of the given CPUs between two cpu_times() samples.

    busy excludes idle, iowait and steal; steal is time the hypervisor ran
    something else while this vCPU wanted to run.
    """
    busy = steal = 0.0
    n = 0
    for c in cpus:
        if c not in t0 or c not in t1:
            continue
        d = [b - a for a, b in zip(t0[c], t1[c], strict=True)]
        total = sum(d)
        if total <= 0:
            continue
        steal += d[7] / total
        busy += (total - d[3] - d[4] - d[7]) / total
        n += 1
    return (busy / n, steal / n) if n else (0.0, 0.0)


QUIET_BUSY = 0.05  # every benchmark CPU below 5% busy before a run starts


def wait_quiet(cfg, results, label: str, cpus: set[int] | None = None,
               timeout: float = 90.0) -> None:
    """Block until the server and client CPUs are idle.

    Catches a previous server that has not finished exiting, a client still
    closing thousands of sockets, or anything else running on the machine.
    Gives up after `timeout` and records the run as noisy.
    """
    if cpus is None:
        cpus = parse_cpus(cfg.server_cpus) | parse_cpus(cfg.client_cpus)
    deadline = time.monotonic() + timeout
    while True:
        t0 = cpu_times()
        time.sleep(1.0)
        t1 = cpu_times()
        worst = max(cpu_usage(t0, t1, {c})[0] for c in cpus)
        if worst < QUIET_BUSY:
            return
        if time.monotonic() > deadline:
            log(f"  WARNING: machine not quiet before {label} (busiest CPU {worst:.0%})")
            results.meta.setdefault("noisy_starts", []).append(f"{label}: {worst:.0%}")
            return


def speed_probe(cfg, results, label: str) -> None:
    """Run probe.py on the server CPU and record the machine's speed right now."""
    cpu = sorted(parse_cpus(cfg.server_cpus))[0]
    out = subprocess.run(["taskset", "-c", str(cpu), sys.executable, str(HERE / "probe.py"), "1"],
                         capture_output=True, text=True)
    try:
        value = int(out.stdout.strip())
    except ValueError:
        return
    results.meta.setdefault("probe", []).append([label, value])


def raise_nofile() -> None:
    _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 200_000
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, max(want, hard)))
    except (ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))


@dataclass
class Server:
    target: str
    port: int
    proc: subprocess.Popen
    log_path: Path
    cpus: str = "0"

    def pids(self) -> list[int]:
        """The server process and its descendants (Granian forks a worker)."""
        out = [self.proc.pid]
        frontier = [self.proc.pid]
        while frontier:
            pid = frontier.pop()
            try:
                children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            except OSError:
                continue
            for child in map(int, children):
                out.append(child)
                frontier.append(child)
        return out

    def cpu_seconds(self) -> float:
        ticks = os.sysconf("SC_CLK_TCK")
        total = 0
        for pid in self.pids():
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            total += int(fields[11]) + int(fields[12])  # utime + stime
        return total / ticks

    def rss_mb(self) -> float:
        total = 0
        for pid in self.pids():
            try:
                for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1])
            except OSError:
                continue
        return total / 1024

    def stop(self) -> None:
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
        # Worker processes (Granian, Sanic, Robyn, Uvicorn --workers) must not
        # outlive the run and eat CPU during the next target's measurement.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(self.proc.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGKILL)


def start_server(target: str, cfg, run_dir: Path, *, compress: bool, workers: int = 1,
                 cpus: str | None = None) -> Server:
    port = cfg.port + TARGETS.index(target)
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((cfg.host, port))
    env = dict(os.environ, PYTHONPATH=str(HERE), BENCH_SSE_INTERVAL=str(cfg.sse_interval))
    env["BENCH_COMPRESS"] = "1" if compress else "0"
    if cfg.gzip_level is not None:
        env["BENCH_GZIP_LEVEL"] = str(cfg.gzip_level)
    if target.startswith("stario"):
        env.update(STARIO_HOST=cfg.host, STARIO_PORT=str(port), STARIO_TRACER="noop")
        if target in ("stario-ft", "stario-ft-loopy"):
            env["STARIO_THREADS"] = str(workers)
        if target in LOOPY:
            # Set explicitly so a silent fallback to epoll fails the launcher.
            env["LOOPYLOOP_BACKEND"] = "epoll" if target.endswith("-epoll") else "uring"
            env["BENCH_LOOP"] = "loopyloop"
        if target == "stario-ft-loopy":
            # loopyloop's extension doesn't declare free-threading support, so importing
            # it would turn the GIL back on. Untested upstream: an experimental row.
            env["PYTHON_GIL"] = "0"
        if not compress:
            for codec in ("ZSTD", "BROTLI", "GZIP"):
                env[f"STARIO_COMPRESS_{codec}_LEVEL"] = "-1"
        elif cfg.gzip_level is not None:
            env["STARIO_COMPRESS_GZIP_LEVEL"] = str(cfg.gzip_level)
    suffix = (".compress" if compress else "") + (f".w{workers}" if workers > 1 else "")
    log_path = run_dir / f"{target}{suffix}.server.log"
    cmd = ["taskset", "-c", cpus or cfg.server_cpus, *server_cmd(target, cfg.host, port, workers)]
    proc = subprocess.Popen(
        cmd, cwd=HERE, env=env, stdout=log_path.open("w"), stderr=subprocess.STDOUT,
        start_new_session=True, preexec_fn=raise_nofile,
    )
    server = Server(target, port, proc, log_path, cpus or cfg.server_cpus)
    deadline = time.monotonic() + 20
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"{target} exited:\n{log_path.read_text()[-3000:]}")
        try:
            with urllib.request.urlopen(f"http://{cfg.host}:{port}/plaintext", timeout=1):
                break
        except OSError:
            if time.monotonic() > deadline:
                server.stop()
                raise RuntimeError(
                    f"{target} not ready:\n{log_path.read_text()[-3000:]}"
                ) from None
            time.sleep(0.1)
    time.sleep(0.3)
    return server


# --- correctness ------------------------------------------------------------


def fetch(url: str, *, data: bytes | None = None, headers=None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def normalize_page(body: bytes) -> str:
    text = body.decode("utf-8")
    text = re.sub(r"(?i)^\s*<!doctype html>", "", text)
    text = re.sub(r">\s+<", "><", text.strip())
    return html.unescape(text)


def check(server: Server, cfg, reference: dict) -> dict:
    """Fetch every endpoint and compare with the expected answers."""
    base = f"http://{cfg.host}:{server.port}"
    problems = []
    status, headers, body = fetch(base + "/plaintext")
    if status != 200 or body != b"Hello, World!":
        problems.append(f"plaintext {status} {body[:60]!r}")
    status, headers, body = fetch(base + "/json")
    if status != 200 or json.loads(body) != {"message": "Hello, World!"}:
        problems.append(f"json {status} {body[:60]!r}")
    status, headers, body = fetch(base + "/user/42")
    if status != 200 or json.loads(body) != {"id": "42", "name": "User 42"}:
        problems.append(f"params {status} {body[:60]!r}")
    status, headers, body = fetch(base + "/validate", data=b'{"name":"Ada","age":42}')
    if status != 200 or json.loads(body) != {"name": "Ada", "age": 42, "valid": True}:
        problems.append(f"validate {status} {body[:60]!r}")
    status, _, body = fetch(base + "/validate", data=b'{"name":"","age":42}')
    if status not in (400, 422):
        problems.append(f"validate-invalid {status}")
    status, headers, body = fetch(base + "/page")
    page = normalize_page(body)
    if status != 200 or "text/html" not in headers.get("content-type", ""):
        problems.append(f"page {status} {headers.get('content-type')}")
    if server.target.startswith("stario"):
        _, _, baked = fetch(base + "/page-baked")
        if normalize_page(baked) != page:
            problems.append("page-baked differs from page")
    if "page" in reference and page != reference["page"]:
        problems.append("page differs from stario's")
    reference.setdefault("page", page)
    if problems:
        raise RuntimeError(f"{server.target} failed checks: {problems}")
    return {"page_bytes": len(body)}


# --- wrk --------------------------------------------------------------------

_UNITS = {"us": 1e-3, "ms": 1.0, "s": 1e3}


def _ms(value: str) -> float:
    m = re.fullmatch(r"([\d.]+)(us|ms|s)", value)
    return float(m.group(1)) * _UNITS[m.group(2)] if m else float("nan")


def _bytes(value: str) -> float:
    m = re.fullmatch(r"([\d.]+)([KMG]?B)", value)
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}[m.group(2)]
    return float(m.group(1)) * mult


@dataclass
class WrkResult:
    rps: float
    p50: float
    p90: float
    p99: float
    bytes_per_req: float
    errors: int
    non2xx: int
    cpu: float  # server CPU utilisation, 1.0 = one core
    steal: float = 0.0  # hypervisor steal on the server CPUs during the run (share)
    client_cpu: float = 0.0  # mean busy share of the client CPUs (1.0 = wrk saturated)


def wrk(server: Server, cfg, path: str, *, conns: int, script: Path | None = None,
        headers: list[str] = (), duration: float | None = None) -> WrkResult:
    duration = duration or cfg.duration
    cmd = [
        "taskset", "-c", cfg.client_cpus, "wrk", "--latency",
        "-t", str(min(cfg.threads, conns)), "-c", str(conns), "-d", f"{int(duration)}s",
        "--timeout", "5s",
    ]
    if script:
        cmd += ["-s", str(script)]
    for header in headers:
        cmd += ["-H", header]
    cmd.append(f"http://{cfg.host}:{server.port}{path}")
    cpu0, t0, st0 = server.cpu_seconds(), time.monotonic(), cpu_times()
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    cpu = (server.cpu_seconds() - cpu0) / (time.monotonic() - t0)
    st1 = cpu_times()
    _, steal = cpu_usage(st0, st1, parse_cpus(server.cpus))
    client_busy, _ = cpu_usage(st0, st1, parse_cpus(cfg.client_cpus))
    rps = float(re.search(r"Requests/sec:\s+([\d.]+)", out).group(1))
    lat = dict(re.findall(r"^\s+(50|90|99)%\s+(\S+)", out, re.M))
    transfer = _bytes(re.search(r"Transfer/sec:\s+(\S+)", out).group(1))
    errors = sum(int(x) for x in re.findall(r"(?:connect|read|write|timeout) (\d+)", out))
    non2xx = int(m.group(1)) if (m := re.search(r"Non-2xx or 3xx responses: (\d+)", out)) else 0
    if server.proc.poll() is not None:
        raise RuntimeError(f"{server.target} died during {path}")
    return WrkResult(rps, _ms(lat.get("50", "nan")), _ms(lat.get("90", "nan")),
                     _ms(lat.get("99", "nan")), transfer / rps if rps else 0, errors, non2xx, cpu,
                     steal, client_busy)


def warmup(server, cfg, path="/plaintext", seconds=2, **kw) -> None:
    wrk(server, cfg, path, conns=32, duration=seconds, **kw)


# --- helper clients ---------------------------------------------------------


def run_client(cfg, server: Server, script: str, *args: str, timeout: float) -> dict:
    """Run a helper client; sample the server's RSS while it runs."""
    cmd = ["taskset", "-c", cfg.client_cpus, sys.executable, str(HERE / script), *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            preexec_fn=raise_nofile)
    peak, deadline = 0.0, time.monotonic() + timeout
    while proc.poll() is None:
        peak = max(peak, server.rss_mb())
        if time.monotonic() > deadline:
            proc.kill()
            raise RuntimeError(f"{script} timed out")
        time.sleep(0.25)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{script} failed: {err[-2000:]}")
    result = json.loads(out.strip().splitlines()[-1])
    result["server_rss_peak_mb"] = round(peak, 1)
    return result


# --- phases -----------------------------------------------------------------

WRK_COUNTED = ("errors", "non2xx")  # summed across rounds; every other number is a median


@dataclass
class Results:
    meta: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)  # phase -> target -> key -> [one dict per round]
    data: dict = field(default_factory=dict)  # the same, reduced across rounds

    def put(self, phase: str, target: str, key: str, value: dict, counted=()) -> None:
        runs = self.raw.setdefault(phase, {}).setdefault(target, {}).setdefault(key, [])
        runs.append(value)
        self.data.setdefault(phase, {}).setdefault(target, {})[key] = reduce_runs(runs, counted)

    def save(self, run_dir: Path) -> None:
        (run_dir / "results.json").write_text(json.dumps(
            {"meta": self.meta, "data": self.data, "raw": self.raw}, indent=1))


def reduce_runs(runs: list[dict], counted=()) -> dict:
    """Each numeric field's median across rounds (sum for `counted` fields).

    With more than one round, `rps_spread` is (max - min) / median of req/s:
    how much the rounds disagreed.
    """
    out = {}
    for k, v in runs[-1].items():
        vals = [r[k] for r in runs if k in r]
        if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vals):
            if k in counted:
                out[k] = sum(vals)
            else:
                m = statistics.median(vals)
                out[k] = round(m, 3) if isinstance(m, float) else m
        else:
            out[k] = v
    out["runs"] = len(runs)
    if "rps" in out and len(runs) > 1 and out["rps"]:
        rps = [r["rps"] for r in runs]
        out["rps_spread"] = round((max(rps) - min(rps)) / statistics.median(rps), 3)
    return out


def as_dict(r: WrkResult) -> dict:
    return {k: round(v, 3) if isinstance(v, float) else v for k, v in r.__dict__.items()}


def fmt_err(r: WrkResult) -> str:
    return f"  ERR {r.errors} non2xx {r.non2xx}" if r.errors or r.non2xx else ""


def measure(server, cfg, results, label, path, **kw) -> WrkResult:
    """Wait for idle CPUs, then one measured wrk run."""
    wait_quiet(cfg, results, label, parse_cpus(server.cpus) | parse_cpus(cfg.client_cpus))
    return wrk(server, cfg, path, **kw)


def warm_all(server, cfg, endpoints: dict) -> None:
    """Exercise every endpoint once before measuring (fresh server per round)."""
    warmup(server, cfg, "/plaintext", seconds=2)
    for name, path in endpoints.items():
        script = HERE / "validate.lua" if name == "validate" else None
        warmup(server, cfg, path, seconds=1, script=script)


def phase_http(target: str, cfg, run_dir: Path, results: Results, phases) -> None:
    """throughput, sweep, mixed: wrk against one fresh server process."""
    server = start_server(target, cfg, run_dir, compress=False)
    try:
        time.sleep(0.5)
        results.put("idle", target, "start", {"rss_mb": round(server.rss_mb(), 1)})
        endpoints = dict(ENDPOINTS)
        if target.startswith("stario"):
            endpoints["page_baked"] = "/page-baked"
        warm_all(server, cfg, endpoints)
        if "throughput" in phases:
            for name, path in endpoints.items():
                script = HERE / "validate.lua" if name == "validate" else None
                r = measure(server, cfg, results, f"{target} {name}", path,
                            conns=cfg.connections, script=script)
                log(f"  {name:<10} {r.rps:>10,.0f} req/s  p50 {r.p50:6.2f} ms  "
                    f"p99 {r.p99:7.2f} ms  cpu {r.cpu:4.2f}  wrk {r.client_cpu:4.2f}  "
                    f"steal {r.steal:4.1%}  {r.bytes_per_req:6.0f} B/req" + fmt_err(r))
                results.put("throughput", target, name, as_dict(r), WRK_COUNTED)
        if "sweep" in phases:
            for conns in cfg.sweep:
                r = measure(server, cfg, results, f"{target} sweep {conns}", "/page", conns=conns)
                log(f"  page c={conns:<5} {r.rps:>10,.0f} req/s  p50 {r.p50:7.2f} ms  "
                    f"p99 {r.p99:8.2f} ms" + fmt_err(r))
                results.put("sweep", target, str(conns), as_dict(r), WRK_COUNTED)
        if "mixed" in phases:
            r = measure(server, cfg, results, f"{target} mixed", "/", conns=cfg.connections,
                        script=HERE / "mixed.lua")
            log(f"  mixed      {r.rps:>10,.0f} req/s  p50 {r.p50:6.2f} ms  p99 {r.p99:7.2f} ms"
                + fmt_err(r))
            results.put("mixed", target, "mixed", as_dict(r), WRK_COUNTED)
    finally:
        server.stop()


def phase_capacity(target: str, cfg, run_dir: Path, results: Results, phases) -> None:
    """sse, conns: many long-lived connections from the helper clients."""
    server = start_server(target, cfg, run_dir, compress=False)
    try:
        warmup(server, cfg, "/page")
        if "sse" in phases and target not in NO_SSE:
            for clients in cfg.sse_clients:
                wait_quiet(cfg, results, f"{target} sse {clients}")
                cpu0, t0 = server.cpu_seconds(), time.monotonic()
                r = run_client(
                    cfg, server, "sse_client.py", f"http://{cfg.host}:{server.port}/sse",
                    str(clients), str(cfg.sse_seconds), timeout=cfg.sse_seconds + 120,
                )
                r["cpu"] = round((server.cpu_seconds() - cpu0) / (time.monotonic() - t0), 3)
                r["rss_mb"] = r.pop("server_rss_peak_mb")
                log(f"  sse n={clients:<6} connected {r['connected']:>6}  "
                    f"p50 {r['p50_ms']:7.1f} ms  p99 {r['p99_ms']:7.1f} ms  "
                    f"delivered {r['delivered_ratio']:.3f}  cpu {r['cpu']:.2f}  rss {r['rss_mb']} MB")
                results.put("sse", target, str(clients), r)
        if "conns" in phases:
            for clients in cfg.conn_clients:
                wait_quiet(cfg, results, f"{target} conns {clients}")
                rss0 = server.rss_mb()
                r = run_client(
                    cfg, server, "conn_client.py", cfg.host, str(server.port), str(clients),
                    str(cfg.conn_seconds), timeout=cfg.conn_seconds + 180,
                )
                r["rss_before_mb"] = round(rss0, 1)
                r["rss_peak_mb"] = r.pop("server_rss_peak_mb", None)
                log(f"  conns n={clients:<6} {r['rps']:>8,.0f} req/s  reconnects {r['reconnects']}  "
                    f"p50 {r['p50_ms']:6.2f} ms  p99 {r['p99_ms']:7.2f} ms  errors {r['errors']}")
                results.put("conns", target, str(clients), r)
    finally:
        server.stop()


def phase_compress(target: str, cfg, run_dir: Path, results: Results, phases) -> None:
    server = start_server(target, cfg, run_dir, compress=True)
    try:
        base = f"http://{cfg.host}:{server.port}/page"
        warmup(server, cfg, "/page", headers=[f"Accept-Encoding: {BROWSER_AE}"])
        cases = [("browser", BROWSER_AE), ("gzip", "gzip")]
        if target.startswith("stario"):
            cases += [("br", "br")]
        for label, ae in cases:
            _, headers, body = fetch(base, headers={"Accept-Encoding": ae})
            coding = headers.get("content-encoding", "identity")
            r = measure(server, cfg, results, f"{target} compress {label}", "/page",
                        conns=cfg.connections, headers=[f"Accept-Encoding: {ae}"])
            log(f"  page[{label}->{coding}] {r.rps:>9,.0f} req/s  p99 {r.p99:6.2f} ms  "
                f"body {len(body):>6} B  wire {r.bytes_per_req:6.0f} B/req" + fmt_err(r))
            d = as_dict(r)
            d.update(coding=coding, body_bytes=len(body))
            results.put("compress", target, label, d, WRK_COUNTED)
        r = measure(server, cfg, results, f"{target} compress mixed", "/",
                    conns=cfg.connections, script=HERE / "mixed.lua")
        log(f"  mixed+compress {r.rps:>9,.0f} req/s  p99 {r.p99:6.2f} ms  "
            f"wire {r.bytes_per_req:6.0f} B/req" + fmt_err(r))
        results.put("compress", target, "mixed", as_dict(r), WRK_COUNTED)
    finally:
        server.stop()


def phase_multicore(target: str, cfg, run_dir: Path, results: Results, phases) -> None:
    """Same endpoints with N workers on N CPUs.

    stario-ft: N event-loop threads in one free-threaded process.
    stario-mp: N GIL processes sharing the port (SO_REUSEPORT).
    """
    # Stario rows also run with one worker: the same-run single-core reference.
    for workers in sorted({1, cfg.mc_workers}) if target in MULTICORE_ONLY else [cfg.mc_workers]:
        server = start_server(target, cfg, run_dir, compress=False, workers=workers,
                              cpus=cfg.mc_cpus)
        key = f"{target} x{workers}"
        try:
            warmup(server, cfg)
            warmup(server, cfg, "/page")
            for name, path, script in (("plaintext", "/plaintext", None), ("page", "/page", None),
                                       ("mixed", "/", HERE / "mixed.lua")):
                r = measure(server, cfg, results, f"{key} {name}", path,
                            conns=cfg.connections, script=script)
                log(f"  [{key}] {name:<9} {r.rps:>9,.0f} req/s  p99 {r.p99:7.2f} ms  "
                    f"cpu {r.cpu:4.2f}  wrk {r.client_cpu:4.2f}" + fmt_err(r))
                results.put("multicore", key, name, as_dict(r), WRK_COUNTED)
        finally:
            server.stop()


# Phases run in groups. Within a group every target gets a fresh server per
# round, and the target order rotates and reverses from round to round, so a
# slow drift of the VM (or the after-effects of one target's run) spreads over
# every target instead of landing on whichever happened to run last.
GROUPS = [
    ("http", {"throughput", "sweep", "mixed"}, phase_http,
     lambda t: t not in MULTICORE_ONLY),
    ("compress", {"compress"}, phase_compress, lambda t: t in COMPRESSING),
    ("capacity", {"sse", "conns"}, phase_capacity, lambda t: t not in MULTICORE_ONLY),
    ("multicore", {"multicore"}, phase_multicore, lambda t: t in MULTICORE),
]


def round_order(targets: list[str], r: int, rounds: int) -> list[str]:
    k = (r * len(targets)) // max(rounds, 1)
    order = targets[k:] + targets[:k]
    return order if r % 2 == 0 else order[::-1]


def isolate_housekeeping(cfg) -> None:
    """Move this process and every other movable process off the benchmark CPUs.

    The housekeeping CPUs are the ones neither the server nor the client
    uses (CPU 1 by default). Original affinities are restored at exit.
    Processes started later inherit their parent's affinity, so tools
    launched by a shell or an agent on this machine stay off the
    benchmark CPUs too.
    """
    bench = parse_cpus(cfg.server_cpus) | parse_cpus(cfg.client_cpus)
    house = set(range(os.cpu_count() or 1)) - bench
    if not house:
        log("  no CPU left for housekeeping; not isolating")
        return
    os.sched_setaffinity(0, house)
    if not cfg.isolate:
        return
    saved: dict[int, set[int]] = {}
    me = os.getpid()
    for task in Path("/proc").glob("[0-9]*/task/[0-9]*"):
        tid = int(task.name)
        try:
            if int(task.parent.parent.name) == me:
                continue
            if not (task.parent.parent / "cmdline").read_bytes():
                continue  # kernel thread
            before = os.sched_getaffinity(tid)
            if before <= house:
                continue
            os.sched_setaffinity(tid, house)
            saved[tid] = before
        except (OSError, ValueError):
            continue
    log(f"  moved {len(saved)} threads to housekeeping CPUs {sorted(house)}")

    def restore() -> None:
        for tid, cpus in saved.items():
            with contextlib.suppress(OSError):
                os.sched_setaffinity(tid, cpus)

    atexit.register(restore)


# --- report -----------------------------------------------------------------


def fmt_int(x) -> str:
    return f"{x:,.0f}" if isinstance(x, (int, float)) else "-"


def write_summary(results: Results, run_dir: Path, targets: list[str]) -> str:
    d = results.data
    lines = ["# Server benchmark", ""]
    lines += [f"- {k}: {v}" for k, v in results.meta.items()
              if k not in ("versions", "order", "noisy_starts", "failures", "probe")]
    for group, orders in results.meta.get("order", {}).items():
        lines.append(f"- {group} order by round: " + " / ".join(" ".join(o) for o in orders))
    lines += ["", "Versions:", ""]
    lines += [f"- {t}: {v}" for t, v in results.meta.get("versions", {}).items()]

    def rel(t, phase, key, metric="rps"):
        try:
            return d[phase][t][key][metric] / d[phase]["stario"][key][metric]
        except (KeyError, ZeroDivisionError):
            return None

    if "throughput" in d:
        lines += ["", "## Throughput (req/s, one CPU, "
                  f"{results.meta['connections']} connections)", ""]
        cols = list(ENDPOINTS)
        lines += ["| Target | " + " | ".join(cols) + " | vs stario (geomean) |",
                  "| --- |" + " ---: |" * (len(cols) + 1)]
        for t in targets:
            row = d["throughput"].get(t)
            if not row:
                continue
            ratios = [rel(t, "throughput", c) for c in cols]
            ratios = [r for r in ratios if r]
            geo = statistics.geometric_mean(ratios) if ratios else None
            lines.append(f"| {t} | " + " | ".join(fmt_int(row[c]["rps"]) for c in cols)
                         + (f" | {geo:.2f}x |" if geo else " | - |"))
        for t in targets:
            if pb := d["throughput"].get(t, {}).get("page_baked"):
                lines += ["", f"{t} `/page-baked` (same page via `@baked`): "
                          f"{pb['rps']:,.0f} req/s, p99 {pb['p99']:.2f} ms "
                          f"({pb['rps'] / d['throughput'][t]['page']['rps']:.2f}x its `/page`)."]
        lines += ["", "p99 latency (ms) at the same load:", "",
                  "| Target | " + " | ".join(cols) + " |", "| --- |" + " ---: |" * len(cols)]
        for t in targets:
            if row := d["throughput"].get(t):
                lines.append(f"| {t} | " + " | ".join(f"{row[c]['p99']:.2f}" for c in cols) + " |")
        lines += ["", "Server CPU (1.00 = one core saturated) and bytes per response:", "",
                  "| Target | " + " | ".join(f"{c} cpu" for c in cols) + " | plaintext B | page B |",
                  "| --- |" + " ---: |" * (len(cols) + 2)]
        for t in targets:
            if row := d["throughput"].get(t):
                lines.append(f"| {t} | " + " | ".join(f"{row[c]['cpu']:.2f}" for c in cols)
                             + f" | {row['plaintext']['bytes_per_req']:.0f}"
                             f" | {row['page']['bytes_per_req']:.0f} |")
    if "sweep" in d:
        conns = sorted({int(c) for t in d["sweep"].values() for c in t})
        lines += ["", "## /page under increasing concurrency (req/s · p99 ms)", "",
                  "| Target | " + " | ".join(f"c={c}" for c in conns) + " |",
                  "| --- |" + " ---: |" * len(conns)]
        for t in targets:
            if row := d["sweep"].get(t):
                cells = []
                for c in conns:
                    r = row.get(str(c))
                    cells.append(f"{r['rps']:,.0f} · {r['p99']:.1f}"
                                 + (f" ({r['errors']} err)" if r["errors"] else "") if r else "-")
                lines.append(f"| {t} | " + " | ".join(cells) + " |")
    if "mixed" in d or "compress" in d:
        lines += ["", "## Browser-shaped mix (40% page, 30% params, 20% json, 10% POST; "
                  "full browser headers + cookie)", "",
                  "| Target | req/s | p50 ms | p99 ms | req/s with compression | wire B/req "
                  "with compression |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for t in targets:
            m = d.get("mixed", {}).get(t, {}).get("mixed")
            c = d.get("compress", {}).get(t, {}).get("mixed")
            if not m and not c:
                continue
            lines.append(f"| {t} | {fmt_int(m['rps'])} | {m['p50']:.2f} | {m['p99']:.2f} | "
                         if m else f"| {t} | - | - | - | ")
            lines[-1] += (f"{fmt_int(c['rps'])} | {c['bytes_per_req']:,.0f} |" if c
                          else "n/a (no built-in compression) | - |")
    if "compress" in d:
        lines += ["", "## /page with compression (browser Accept-Encoding unless noted)", "",
                  "| Target | case | coding | body B | req/s | p99 ms |",
                  "| --- | --- | --- | ---: | ---: | ---: |"]
        for t in targets:
            for label, r in d["compress"].get(t, {}).items():
                if label == "mixed":
                    continue
                lines.append(f"| {t} | {label} | {r['coding']} | {r['body_bytes']:,} | "
                             f"{fmt_int(r['rps'])} | {r['p99']:.2f} |")
    if "sse" in d:
        lines += ["", "## SSE fan-out (Datastar patch every "
                  f"{results.meta['sse_interval']} s to N clients)", "",
                  "| Target | clients | connected | delivered | p50 ms | p99 ms | max ms | "
                  "server CPU | RSS MB |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for t in targets:
            for n, r in d["sse"].get(t, {}).items():
                lines.append(f"| {t} | {int(n):,} | {r['connected']:,} | {r['delivered_ratio']:.3f} | "
                             f"{r['p50_ms']:.1f} | {r['p99_ms']:.1f} | {r['max_ms']:.1f} | "
                             f"{r['cpu']:.2f} | {r['rss_mb']:.0f} |")
    if "conns" in d:
        lines += ["", "## Connected clients (each sends GET /page every 2 s on its own "
                  "keep-alive connection)", "",
                  "Achieved req/s counts the connect ramp, so ~90% of offered is a full pass. "
                  "RSS is the whole server after the earlier phases (the heap is reused, "
                  "so the growth is not a per-connection cost).", "",
                  "| Target | clients | offered req/s | achieved req/s | p50 ms | p99 ms | errors | "
                  "reconnects | RSS before MB | RSS peak MB |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for t in targets:
            for n, r in d["conns"].get(t, {}).items():
                lines.append(f"| {t} | {int(n):,} | {int(n) / 2:,.0f} | {fmt_int(r['rps'])} | "
                             f"{r['p50_ms']:.2f} | {r['p99_ms']:.2f} | {r['errors']} | "
                             f"{r.get('reconnects', 0)} | "
                             f"{r['rss_before_mb']:.0f} | {r['rss_peak_mb'] or 0:.0f} |")
    if "multicore" in d:
        lines += ["", f"## Multi-core ({results.meta.get('mc_workers')} workers on CPUs "
                  f"{results.meta.get('mc_cpus')}; stario-ft = free-threaded 3.14t, "
                  "one event loop per thread; stario-mp = N GIL processes on SO_REUSEPORT)", "",
                  "| Target | plaintext | page | mixed | p99 page ms | CPU (page) |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for key, row in d["multicore"].items():
            lines.append(f"| {key} | {fmt_int(row['plaintext']['rps'])} | {fmt_int(row['page']['rps'])} | "
                         f"{fmt_int(row['mixed']['rps'])} | {row['page']['p99']:.2f} | "
                         f"{row['page']['cpu']:.2f} |")
    if "idle" in d:
        lines += ["", "## Footprint", "", "| Target | RSS after start MB | /page bytes |",
                  "| --- | ---: | ---: |"]
        for t in targets:
            if r := d["idle"].get(t):
                rss = r.get("start", {}).get("rss_mb")
                size = r.get("page", {}).get("bytes")
                lines.append(f"| {t} | {f'{rss:.0f}' if rss is not None else '-'} | "
                             f"{f'{size:,}' if size else '-'} |")
    wrk_runs = [run for phase in ("throughput", "sweep", "mixed", "compress", "multicore")
                for tgt in results.raw.get(phase, {}).values()
                for runs in tgt.values() for run in runs if "steal" in run]
    if wrk_runs:
        lines += ["", "## Run quality", "",
                  f"- wrk runs: {len(wrk_runs)}; rounds per measurement: "
                  f"{results.meta.get('rounds')}",
                  f"- hypervisor steal on the server CPU: median "
                  f"{statistics.median(r['steal'] for r in wrk_runs):.2%}, max "
                  f"{max(r['steal'] for r in wrk_runs):.2%}",
                  f"- load-generator CPUs busy: median "
                  f"{statistics.median(r['client_cpu'] for r in wrk_runs):.0%}, max "
                  f"{max(r['client_cpu'] for r in wrk_runs):.0%}",
                  *([f"- machine speed probe (probe.py on the server CPU before each target): "
                     f"min {min(v for _, v in probes):,}, median "
                     f"{statistics.median(v for _, v in probes):,.0f}, max {max(v for _, v in probes):,} "
                     f"units/s ({(max(v for _, v in probes) - min(v for _, v in probes)) / statistics.median(v for _, v in probes):.0%} range)"]
                    if (probes := results.meta.get("probe")) else []),
                  f"- runs started on a machine that was not idle: "
                  f"{len(results.meta.get('noisy_starts', []))}",
                  f"- failures: {results.meta.get('failures') or 'none'}"]
    if "throughput" in d:
        cols = list(ENDPOINTS)
        lines += ["", "Spread of req/s across rounds, (max - min) / median:", "",
                  "| Target | " + " | ".join(cols) + " |", "| --- |" + " ---: |" * len(cols)]
        for t in targets:
            if row := d["throughput"].get(t):
                lines.append(f"| {t} | " + " | ".join(
                    f"{row[c].get('rps_spread', 0):.1%}" if c in row else "-" for c in cols) + " |")
    text = "\n".join(lines) + "\n"
    (run_dir / "summary.md").write_text(text)
    return text


# --- main -------------------------------------------------------------------


def main() -> None:
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", help="targets (default: all)")
    ap.add_argument("--phases", default=env("PHASES", "throughput,sweep,mixed,compress,sse,conns"))
    ap.add_argument("--duration", type=float, default=float(env("DURATION", "10s").rstrip("s")))
    ap.add_argument("--rounds", type=int, default=int(env("ROUNDS", "3")),
                    help="measurements per number; each round restarts every server and "
                         "rotates the target order (the median is reported)")
    ap.add_argument("--capacity-rounds", type=int,
                    default=int(env("CAPACITY_ROUNDS")) if env("CAPACITY_ROUNDS") else None,
                    help="rounds for the sse/conns phases (default: --rounds)")
    ap.add_argument("--threads", type=int, default=int(env("THREADS", "2")))
    ap.add_argument("--connections", type=int, default=int(env("CONNECTIONS", "128")))
    ap.add_argument("--sweep", default=env("SWEEP", "8,64,256,1024"))
    ap.add_argument("--sse-clients", default=env("SSE_CLIENTS", "1000,5000"))
    ap.add_argument("--sse-seconds", type=float, default=float(env("SSE_SECONDS", "15")))
    ap.add_argument("--sse-interval", type=float, default=float(env("SSE_INTERVAL", "0.5")))
    ap.add_argument("--conn-clients", default=env("CONN_CLIENTS", "2000,10000"))
    ap.add_argument("--conn-seconds", type=float, default=float(env("CONN_SECONDS", "20")))
    ap.add_argument("--gzip-level", type=int, default=int(env("GZIP_LEVEL")) if env("GZIP_LEVEL") else None,
                    help="force one gzip level in every framework (compress phase)")
    ap.add_argument("--mc-workers", type=int, default=int(env("MC_WORKERS", "2")))
    ap.add_argument("--mc-cpus", default=env("MC_CPUS", "0,1"))
    ap.add_argument("--host", default=env("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env("PORT", "3000")))
    ap.add_argument("--server-cpus", default=env("SERVER_CPUS", "0"))
    ap.add_argument("--client-cpus", default=env("CLIENT_CPUS", "2,3"))
    ap.add_argument("--python", default=env("PYTHON", "3.14"))
    ap.add_argument("--isolate", action="store_true", default=env("ISOLATE") == "1",
                    help="move every other process on the machine to the CPUs neither "
                         "the server nor the client uses, for the length of the run")
    ap.add_argument("--refresh-envs", action="store_true", default=env("REFRESH_ENVS") == "1")
    cfg = ap.parse_args()
    cfg.sweep = [int(x) for x in cfg.sweep.split(",") if x]
    cfg.sse_clients = [int(x) for x in cfg.sse_clients.split(",") if x]
    cfg.conn_clients = [int(x) for x in cfg.conn_clients.split(",") if x]
    phases = set(cfg.phases.split(","))
    targets = []
    default = [t for t in TARGETS if t not in MULTICORE_ONLY | LOOPY]
    if "multicore" in phases:
        default += sorted(MULTICORE_ONLY - LOOPY)
    for t in cfg.targets or default:
        targets += t.split(",")
    for t in targets:
        if t not in TARGETS:
            sys.exit(f"unknown target {t}; choose from {', '.join(TARGETS)}")
    if "stario" not in targets and phases - {"multicore"}:
        targets.insert(0, "stario")  # the reference page and the baseline column
    for tool in ("uv", "wrk", "taskset"):
        if not shutil.which(tool):
            sys.exit(f"missing required command: {tool}")
    raise_nofile()

    run_dir = RESULTS / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir.mkdir(parents=True)
    results = Results()
    results.meta = {
        "duration_s": cfg.duration, "rounds": cfg.rounds,
        "capacity_rounds": cfg.capacity_rounds or cfg.rounds, "connections": cfg.connections,
        "wrk_threads": cfg.threads, "server_cpus": cfg.server_cpus, "client_cpus": cfg.client_cpus,
        "cpu": next((line.split(":", 1)[1].strip()
                     for line in Path("/proc/cpuinfo").read_text().splitlines()
                     if line.startswith("model name")), "?"),
        "nproc": os.cpu_count(), "sse_interval": cfg.sse_interval,
        "gzip_level": cfg.gzip_level or "framework default", "versions": {},
        "mc_workers": cfg.mc_workers, "mc_cpus": cfg.mc_cpus,
        "started": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
    }
    log(f"Writing results to {run_dir.relative_to(ROOT)}")

    # Setup first: building environments is heavy and must not overlap a run.
    for target in targets:
        ensure_env(target, cfg.python, cfg.refresh_envs)
        results.meta["versions"][target] = versions(target)
        log(f"  {target}: {results.meta['versions'][target]}")
    isolate_housekeeping(cfg)

    # Correctness: every target answers every endpoint like Stario does.
    reference: dict = {}
    for target in targets:
        server = start_server(target, cfg, run_dir, compress=False)
        try:
            info = check(server, cfg, reference)
        finally:
            server.stop()
        results.put("idle", target, "page", {"bytes": info["page_bytes"]})
        log(f"  {target}: checks passed")

    t_start = time.monotonic()
    for group, group_phases, run, applies in GROUPS:
        if not phases & group_phases:
            continue
        group_targets = [t for t in targets if applies(t)]
        rounds = (cfg.capacity_rounds or cfg.rounds) if group == "capacity" else cfg.rounds
        for r in range(rounds):
            order = round_order(group_targets, r, rounds)
            results.meta.setdefault("order", {}).setdefault(group, []).append(order)
            log(f"\n== {group}, round {r + 1}/{rounds} "
                f"({(time.monotonic() - t_start) / 60:.0f} min in): {' '.join(order)}")
            for target in order:
                wait_quiet(cfg, results, f"{group} r{r + 1} {target} probe")
                speed_probe(cfg, results, f"{group} r{r + 1} {target}")
                log(f"-- {target}  (machine speed probe: {results.meta['probe'][-1][1]:,}/s)"
                    if results.meta.get("probe") else f"-- {target}")
                try:
                    run(target, cfg, run_dir, results, phases)
                except Exception as exc:  # keep the other targets' numbers
                    log(f"  FAILED: {exc}")
                    results.meta.setdefault("failures", []).append(
                        f"{group} r{r + 1} {target}: {str(exc).splitlines()[0][:200]}")
                results.save(run_dir)
            write_summary(results, run_dir, targets)
    results.meta["finished"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    results.save(run_dir)
    log("")
    log(write_summary(results, run_dir, targets))
    for p in run_dir.glob("*.server.log"):
        if p.stat().st_size == 0:
            p.unlink()
    log(f"Summary: {(run_dir / 'summary.md').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
