#!/usr/bin/env bash
# Thin wrapper kept for muscle memory: runs bench.py's throughput phase
# (plaintext, json, params, validate, page). For every phase, run bench.py
# directly. Environment knobs (DURATION, THREADS, CONNECTIONS, PORT, PYTHON,
# REFRESH_ENVS, PHASES, ...) are read by bench.py.
set -euo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/bench.py" --phases "${PHASES:-throughput}" "$@"
