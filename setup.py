"""Build the Cython HTTP runtime (``stario_cython``).

Native libraries come from pkg-config: nghttp2 1.66+ (MadeYouReset,
CVE-2025-8671), Brotli with the CVE-2026-111497 fix (1.0.10+, 1.1.1+, or
1.2.1+), and zstd. Gzip links system zlib. llhttp is vendored.
A distro nghttp2 older than 1.66 that carries the security backports can be
used with ``STARIO_ALLOW_OLD_NGHTTP2=1``. ``STARIO_ALLOW_OLD_BROTLI=1`` is
the same escape for a Brotli package that backports CVE-2026-111497 without
a version bump.

Development build: ``uv pip install -e .`` or ``python setup.py build_ext --inplace``.
"""

import os
import platform
import shlex
import subprocess
import sys

from Cython.Build import cythonize
from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

_NATIVE_PACKAGES = (
    "libnghttp2",
    "libbrotlienc",
    "libbrotlidec",
    "libbrotlicommon",
    "libzstd",
)
_MIN_NGHTTP2 = (1, 66)


def _pkg_config(option: str) -> list[str]:
    try:
        result = subprocess.run(
            ["pkg-config", option, *_NATIVE_PACKAGES],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeError(
            "Building stario needs pkg-config plus the nghttp2, Brotli, and "
            "zstd development packages (Debian/Ubuntu: libnghttp2-dev "
            "libbrotli-dev libzstd-dev; macOS: brew install pkg-config "
            "nghttp2 brotli zstd). "
            f"pkg-config detail: {detail.strip()}"
        ) from exc
    return shlex.split(result.stdout)


def _version_tuple(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in raw.strip().split("."):
        digits = ""
        for char in part:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _brotli_has_cve_2026_111497_fix(version: tuple[int, ...]) -> bool:
    """True for a libbrotli release outside the CVE-2026-111497 range.

    Fixed lines are 1.0.10+, 1.1.1+, and 1.2.1+. Later majors and minors
    count as fixed. 1.0.3 through 1.0.9, 1.1.0, and 1.2.0 do not.
    """
    if len(version) < 2:
        return False
    major, minor = version[0], version[1]
    patch = version[2] if len(version) > 2 else 0
    if major > 1 or (major == 1 and minor > 2):
        return True
    if (major, minor) == (1, 0):
        return patch >= 10
    if (major, minor) == (1, 1):
        return patch >= 1
    if (major, minor) == (1, 2):
        return patch >= 1
    return False


def _require_brotli_version() -> None:
    if os.environ.get("STARIO_ALLOW_OLD_BROTLI") == "1":
        return
    raw = subprocess.run(
        ["pkg-config", "--modversion", "libbrotlidec"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if _brotli_has_cve_2026_111497_fix(_version_tuple(raw)):
        return
    raise RuntimeError(
        f"stario needs a Brotli decoder with the CVE-2026-111497 fix "
        f"(found {raw}). Affected releases are 1.0.3-1.0.9, 1.1.0, and "
        "1.2.0. Install 1.0.10+, 1.1.1+, or 1.2.1+ "
        "(scripts/build-native-deps.sh), or set STARIO_ALLOW_OLD_BROTLI=1 "
        "only if your distro backports that fix."
    )


def _require_nghttp2_version() -> None:
    if os.environ.get("STARIO_ALLOW_OLD_NGHTTP2") == "1":
        return
    raw = subprocess.run(
        ["pkg-config", "--modversion", "libnghttp2"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    version = tuple(int(part) for part in raw.split(".")[:2] if part.isdigit())
    if version < _MIN_NGHTTP2:
        wanted = ".".join(map(str, _MIN_NGHTTP2))
        raise RuntimeError(
            f"stario needs nghttp2 {wanted}+ (found {raw}): older releases lack "
            "the MadeYouReset (CVE-2025-8671) fix. Install a newer libnghttp2, "
            "build one with scripts/build-native-deps.sh, or set "
            "STARIO_ALLOW_OLD_NGHTTP2=1 if your distro backports the fix."
        )


class native_build_ext(build_ext):
    """Resolve pkg-config flags at compile time so sdist/metadata need no libs."""

    def build_extensions(self) -> None:
        _pkg_config("--exists")
        _require_nghttp2_version()
        _require_brotli_version()
        include_dirs = [
            f[2:] for f in _pkg_config("--cflags-only-I") if f.startswith("-I")
        ]
        library_dirs = [
            f[2:] for f in _pkg_config("--libs-only-L") if f.startswith("-L")
        ]
        libraries = [f[2:] for f in _pkg_config("--libs-only-l") if f.startswith("-l")]
        if "z" not in libraries:
            libraries.append("z")
        compile_args = _pkg_config("--cflags-only-other")
        link_args = _pkg_config("--libs-only-other")
        for ext in self.extensions:
            ext.include_dirs.extend(include_dirs)
            ext.library_dirs.extend(library_dirs)
            ext.libraries.extend(libraries)
            ext.extra_compile_args.extend(compile_args)
            ext.extra_link_args.extend(link_args)
        super().build_extensions()


# -g0 overrides the -g that Python's own CFLAGS add: a wheel ships no debug info.
# STARIO_DEBUG_BUILD=1 keeps debug info and symbols for gdb or perf.
debug_build = os.environ.get("STARIO_DEBUG_BUILD") == "1"
base_args = ["-O3", "-fno-strict-aliasing", "-g" if debug_build else "-g0"]
# The linker drops the static symbol table too; Python finds PyInit_* in .dynsym.
strip_args = ["-s"] if sys.platform.startswith("linux") and not debug_build else []
llhttp_sse = ["-msse4.2"] if platform.machine().lower() in {"x86_64", "amd64"} else []
include_dirs = ["vendor", "vendor/llhttp/include", "src"]

extensions = [
    Extension(
        "stario_cython.codecs",
        sources=["src/stario_cython/codecs.pyx", "vendor/compression_buf.c"],
        include_dirs=list(include_dirs),
        extra_compile_args=list(base_args),
        extra_link_args=list(strip_args),
    ),
    Extension(
        "stario_cython.exchange",
        sources=["src/stario_cython/exchange.pyx", "vendor/compression_buf.c"],
        include_dirs=list(include_dirs),
        extra_compile_args=list(base_args),
        extra_link_args=list(strip_args),
    ),
    Extension(
        "stario_cython.xxh64",
        sources=["src/stario_cython/xxh64.pyx"],
        extra_compile_args=list(base_args),
        extra_link_args=list(strip_args),
    ),
    Extension(
        "stario_cython.markup",
        sources=["src/stario_cython/markup.pyx"],
        extra_compile_args=list(base_args),
        extra_link_args=list(strip_args),
    ),
    Extension(
        "stario_cython.protocol",
        sources=[
            "src/stario_cython/protocol.pyx",
            "vendor/llhttp/src/llhttp.c",
            "vendor/llhttp/src/http.c",
            "vendor/llhttp/src/api.c",
            "vendor/llhttp/src/stario_alloc.c",
        ],
        include_dirs=list(include_dirs),
        extra_compile_args=[*base_args, *llhttp_sse],
        extra_link_args=list(strip_args),
    ),
]

setup(
    ext_modules=cythonize(extensions, compiler_directives={"language_level": 3}),
    cmdclass={"build_ext": native_build_ext},
)
