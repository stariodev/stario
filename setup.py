"""Build the Cython HTTP runtime (``stario_cython``).

Native libraries come from pkg-config: nghttp2 1.66+ (MadeYouReset,
CVE-2025-8671), Brotli with the CVE-2026-111497 fix (1.0.10+, 1.1.1+, or
1.2.1+), and zstd. Gzip links system zlib (on Windows, zlib from pkg-config
too). llhttp is vendored. ``PKG_CONFIG`` names the pkg-config binary (default
``pkg-config``); on Windows, scripts/build-native-deps.ps1 builds every
library with vcpkg and prints the variables to set.
A distro nghttp2 older than 1.66 that carries the security backports can be
used with ``STARIO_ALLOW_OLD_NGHTTP2=1``. ``STARIO_ALLOW_OLD_BROTLI=1`` is
the same escape for a Brotli package that backports CVE-2026-111497 without
a version bump.

Development build: ``uv pip install -e .`` or ``python setup.py build_ext --inplace``.

``STARIO_PGO=1`` (GCC only) builds twice: once instrumented, a run of
``scripts/pgo_train.py`` against that build, then the real build with the
recorded profile. If the compiler is not GCC or training fails, the build
goes on without a profile.
"""

import glob
import os
import platform
import shlex
import shutil
import subprocess
import sys

from Cython.Build import cythonize
from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

WINDOWS = sys.platform == "win32"
# Windows has no system zlib: vcpkg installs one with a zlib.pc.
_NATIVE_PACKAGES = (
    "libnghttp2",
    "libbrotlienc",
    "libbrotlidec",
    "libbrotlicommon",
    "libzstd",
    *(("zlib",) if WINDOWS else ()),
)
_MIN_NGHTTP2 = (1, 66)
_PKG_CONFIG = os.environ.get("PKG_CONFIG") or "pkg-config"


def _pkg_config(option: str) -> list[str]:
    try:
        result = subprocess.run(
            [_PKG_CONFIG, option, *_NATIVE_PACKAGES],
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
            "nghttp2 brotli zstd; Windows: scripts/build-native-deps.ps1, "
            "then set PKG_CONFIG and PKG_CONFIG_PATH as it prints). "
            f"pkg-config detail: {detail.strip()}"
        ) from exc
    # POSIX rules on Windows too: pkgconf escapes `\\` and spaces in paths.
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
        [_PKG_CONFIG, "--modversion", "libbrotlidec"],
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
        [_PKG_CONFIG, "--modversion", "libnghttp2"],
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
        if not WINDOWS and "z" not in libraries:
            libraries.append("z")
        compile_args = _pkg_config("--cflags-only-other")
        link_args = _pkg_config("--libs-only-other")
        msvc = self.compiler.compiler_type == "msvc"
        for ext in self.extensions:
            own_compile, own_link = _compiler_args(
                msvc=msvc, sse42=ext.name == "stario_cython.protocol"
            )
            ext.include_dirs.extend(include_dirs)
            ext.library_dirs.extend(library_dirs)
            ext.libraries.extend(libraries)
            ext.extra_compile_args.extend([*own_compile, *compile_args])
            ext.extra_link_args.extend([*own_link, *link_args])
        if os.environ.get("STARIO_PGO") == "1" and not msvc and self._is_gcc():
            self._build_extensions_with_profile()
        else:
            super().build_extensions()

    def _is_gcc(self) -> bool:
        # Clang writes .profraw files that need llvm-profdata; not handled.
        try:
            banner = subprocess.run(
                [self.compiler.compiler_so[0], "--version"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError, IndexError, AttributeError):
            return False
        return "Free Software Foundation" in banner

    def _build_extensions_with_profile(self) -> None:
        """Instrumented build, training run, then the build that uses the profile.

        GCC keeps each object's counters (.gcda) next to the object in
        build_temp, and both passes compile to the same object paths.
        """
        for stale in glob.glob(
            os.path.join(self.build_temp, "**", "*.gcda"), recursive=True
        ):
            os.remove(stale)
        generate = ["-fprofile-generate", "-fprofile-update=atomic"]
        use = ["-fprofile-use", "-fprofile-correction", "-Wno-missing-profile"]
        build_lib, inplace, force = self.build_lib, self.inplace, self.force
        train_lib = os.path.join(self.build_temp, "pgo-lib")
        for ext in self.extensions:
            ext.extra_compile_args.extend(generate)
            ext.extra_link_args.append("-fprofile-generate")
        self.build_lib, self.inplace, self.force = train_lib, 0, 1
        try:
            super().build_extensions()
            trained = self._train(train_lib)
        finally:
            self.build_lib, self.inplace = build_lib, inplace
            for ext in self.extensions:
                del ext.extra_compile_args[-len(generate) :]
                del ext.extra_link_args[-1]
        if trained:
            for ext in self.extensions:
                ext.extra_compile_args.extend(use)
        else:
            print("stario: PGO training failed; building without a profile")
        super().build_extensions()
        self.force = force

    def _train(self, train_lib: str) -> bool:
        script = os.path.join("scripts", "pgo_train.py")
        if not os.path.exists(script):
            return False
        # The instrumented extensions, under the package's own __init__.py.
        package = os.path.join(train_lib, "stario_cython")
        shutil.copy(os.path.join("src", "stario_cython", "__init__.py"), package)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([train_lib, "src"])
        env["STARIO_TRACER"] = "noop"
        try:
            subprocess.run(
                [sys.executable, script],
                check=True,
                env=env,
                timeout=900,
                stdout=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"stario: PGO training run: {exc}")
            return False
        return True


# STARIO_DEBUG_BUILD=1 keeps debug info and symbols for gdb, perf, or WinDbg.
debug_build = os.environ.get("STARIO_DEBUG_BUILD") == "1"


def _compiler_args(*, msvc: bool, sse42: bool) -> tuple[list[str], list[str]]:
    """Compile and link flags for one extension, by compiler family."""
    if msvc:
        # setuptools already passes /O2 /GL /MD /DNDEBUG, and MSVC does no
        # type-based alias analysis, so nothing mirrors -fno-strict-aliasing.
        # llhttp's SSE 4.2 path keys on __SSE4_2__, which MSVC never defines.
        return (["/Zi"], ["/DEBUG"]) if debug_build else ([], [])
    # -g0 overrides the -g that Python's own CFLAGS add: a wheel ships no debug info.
    compile_args = ["-O3", "-fno-strict-aliasing", "-g" if debug_build else "-g0"]
    # Hardening the interpreter's own CFLAGS may or may not carry (manylinux
    # Pythons do not): stack canaries on functions with a char buffer, and
    # checked libc string/memory calls where the object size is known.
    # Not -fstack-protector-strong: it adds a canary to every function with
    # an address-taken local, which is most of Cython's output, and cost 5%
    # on a plaintext request.
    compile_args += [
        "-fstack-protector",
        "-U_FORTIFY_SOURCE",
        "-D_FORTIFY_SOURCE=2",
    ]
    if sse42 and platform.machine().lower() in {"x86_64", "amd64"}:
        compile_args.append("-msse4.2")
    # The linker drops the static symbol table too; Python finds PyInit_* in .dynsym.
    link_args = ["-s"] if sys.platform.startswith("linux") and not debug_build else []
    if sys.platform.startswith("linux"):
        # Read-only relocations, resolved at load; no executable stack.
        link_args += ["-Wl,-z,relro", "-Wl,-z,now", "-Wl,-z,noexecstack"]
    return compile_args, link_args


include_dirs = ["vendor", "vendor/llhttp/include", "src"]

extensions = [
    Extension(
        "stario_cython.codecs",
        sources=["src/stario_cython/codecs.pyx", "vendor/compression_buf.c"],
        include_dirs=list(include_dirs),
    ),
    Extension(
        "stario_cython.exchange",
        sources=["src/stario_cython/exchange.pyx", "vendor/compression_buf.c"],
        include_dirs=list(include_dirs),
    ),
    Extension(
        "stario_cython.xxh64",
        sources=["src/stario_cython/xxh64.pyx"],
    ),
    Extension(
        "stario_cython.markup",
        sources=["src/stario_cython/markup.pyx"],
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
    ),
]

setup(
    ext_modules=cythonize(extensions, compiler_directives={"language_level": 3}),
    cmdclass={"build_ext": native_build_ext},
)
