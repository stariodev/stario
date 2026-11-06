"""Internal terminal compatibility and ANSI styling for CLI and telemetry output."""

import os
import sys
from typing import TextIO

_WIN32_VT_ENABLED = False

RESET = "\033[0m"
SGR: dict[str, str] = {
    "reset": RESET,
    "dim": "\033[2m",
    "bold": "\033[1m",
    "underline": "\033[4m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
}


def color_enabled() -> bool:
    # https://no-color.org/ — any value disables color
    return "NO_COLOR" not in os.environ


def enable_windows_console_vt() -> None:
    """Enable escape processing on process stdout (Windows 10+). Safe to call repeatedly.

    Targets STD_OUTPUT_HANDLE only; custom streams are unchanged. Failures are ignored.
    """
    global _WIN32_VT_ENABLED
    if _WIN32_VT_ENABLED or sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # pyrefly: ignore[missing-attribute]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            if kernel32.SetConsoleMode(handle, mode.value | enable_vt):
                _WIN32_VT_ENABLED = True
    except (OSError, AttributeError):
        pass


def enable_vt_for_stream(stream: TextIO) -> None:
    if stream is sys.stdout or stream is sys.stderr:
        enable_windows_console_vt()
