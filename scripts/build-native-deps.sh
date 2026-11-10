#!/usr/bin/env bash
# Build the native libraries stario links (nghttp2, Brotli, zstd) into PREFIX.
# Used by cibuildwheel: manylinux ships an nghttp2 without
# nghttp2_option_set_max_continuations, and Homebrew bottles target the
# runner's macOS rather than MACOSX_DEPLOYMENT_TARGET.
#
# Usage: build-native-deps.sh [PREFIX] [all|nghttp2,brotli,zstd]
# Brotli is 1.2.1 (CVE-2026-111497). CI builds only brotli so the distro
# nghttp2 stays the one under test.
set -euo pipefail

PREFIX="${1:-/usr/local}"
COMPONENTS="${2:-all}"
NGHTTP2_VERSION="1.70.0"
BROTLI_VERSION="1.2.1"
ZSTD_VERSION="1.5.7"
# nghttp2: GitHub release asset digest. zstd: published .sha256 next to the
# tarball. brotli publishes no hash for tag archives — pinned for v1.2.1.
NGHTTP2_SHA256="e05cb1388eaca3830aded4ccf20044b6e1ac1a61411dcca11b0437c4285c8bc2"
BROTLI_SHA256="98d35d11a78dc453aebb7d10c3748f3f73ff78c8267c0dc697b8631f67bf7e8f"
ZSTD_SHA256="eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3"
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"

# The wheels bundle these libraries, and nghttp2 parses every HTTP/2 byte a
# client sends. Build them with the same hardening as the extensions
# (setup.py): stack canaries, checked libc calls, stack clash probes, full
# RELRO. No debug info: autotools' default `-g -O2` put 0.5 MB of DWARF into
# the bundled libnghttp2. nghttp2's own assert()s stay on (no -DNDEBUG): an
# internal invariant that fails should abort, not run on. CMake Release
# builds append their -O3 -DNDEBUG after these flags. Linux only; Apple's
# toolchain defaults already cover what applies there.
if [[ "$(uname -s)" == "Linux" ]]; then
  export CFLAGS="-O2 -g0 -fstack-protector -fstack-clash-protection -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=2${CFLAGS:+ $CFLAGS}"
  export LDFLAGS="-Wl,-z,relro -Wl,-z,now -Wl,-z,noexecstack${LDFLAGS:+ $LDFLAGS}"
fi

want() {
  local name="$1" part
  if [[ "$COMPONENTS" == "all" ]]; then
    return 0
  fi
  local IFS=','
  for part in $COMPONENTS; do
    if [[ "$part" == "$name" ]]; then
      return 0
    fi
  done
  return 1
}

if [[ "$COMPONENTS" != "all" ]]; then
  IFS=',' read -ra _requested <<< "$COMPONENTS"
  if [[ ${#_requested[@]} -eq 0 ]]; then
    echo "build-native-deps.sh: no components named" >&2
    exit 1
  fi
  for part in "${_requested[@]}"; do
    case "$part" in
      nghttp2|brotli|zstd) ;;
      *)
        echo "build-native-deps.sh: unknown component '$part' (nghttp2, brotli, zstd, or all)" >&2
        exit 1
        ;;
    esac
  done
fi

verify_sha256() {
  local file="$1" expected="$2" actual
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$file" | cut -d' ' -f1)"
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$file" | cut -d' ' -f1)"
  else
    echo "no SHA-256 tool (need sha256sum or shasum)" >&2
    exit 1
  fi
  if [[ "$actual" != "$expected" ]]; then
    echo "SHA-256 mismatch for $file: expected $expected, got $actual" >&2
    exit 1
  fi
}

SUDO=""
if [[ ! -w "$(dirname "$PREFIX")" && "$(id -u)" != 0 ]]; then
  SUDO="sudo"
fi
$SUDO mkdir -p "$PREFIX"

# Download with retries: GitHub release and archive downloads time out, 504,
# or break mid-transfer now and then. A loop rather than curl's
# --retry-all-errors, which the manylinux container's curl (7.61) lacks.
fetch() {
  local out="$1" url="$2" attempt
  for attempt in 1 2 3 4 5; do
    # HTTPS only, redirects included (the SHA-256 check is the real guard).
    if curl -fsSL --proto '=https' --proto-redir '=https' --retry 2 --retry-delay 5 -o "$out" "$url"; then
      return 0
    fi
    echo "build-native-deps.sh: download failed (attempt $attempt of 5): $url" >&2
    sleep $((attempt * 5))
  done
  return 1
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"

if want nghttp2; then
  fetch nghttp2.tar.xz \
    "https://github.com/nghttp2/nghttp2/releases/download/v${NGHTTP2_VERSION}/nghttp2-${NGHTTP2_VERSION}.tar.xz"
  verify_sha256 nghttp2.tar.xz "$NGHTTP2_SHA256"
  tar xf nghttp2.tar.xz
  (
    cd "nghttp2-${NGHTTP2_VERSION}"
    ./configure --prefix="$PREFIX" --libdir="$PREFIX/lib" --enable-lib-only \
      --disable-static --disable-dependency-tracking >/dev/null
    make -j"$JOBS" >/dev/null
    $SUDO make install >/dev/null
  )
fi

if want brotli; then
  fetch brotli.tar.gz \
    "https://github.com/google/brotli/archive/refs/tags/v${BROTLI_VERSION}.tar.gz"
  verify_sha256 brotli.tar.gz "$BROTLI_SHA256"
  tar xf brotli.tar.gz
  cmake -S "brotli-${BROTLI_VERSION}" -B brotli-build \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_INSTALL_LIBDIR=lib \
    -DBROTLI_DISABLE_TESTS=ON >/dev/null
  cmake --build brotli-build -j"$JOBS" >/dev/null
  $SUDO cmake --install brotli-build >/dev/null
fi

if want zstd; then
  fetch zstd.tar.gz \
    "https://github.com/facebook/zstd/releases/download/v${ZSTD_VERSION}/zstd-${ZSTD_VERSION}.tar.gz"
  verify_sha256 zstd.tar.gz "$ZSTD_SHA256"
  tar xf zstd.tar.gz
  cmake -S "zstd-${ZSTD_VERSION}/build/cmake" -B zstd-build \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_INSTALL_LIBDIR=lib \
    -DZSTD_BUILD_PROGRAMS=OFF \
    -DZSTD_BUILD_TESTS=OFF \
    -DZSTD_BUILD_STATIC=OFF \
    -DZSTD_BUILD_SHARED=ON >/dev/null
  cmake --build zstd-build -j"$JOBS" >/dev/null
  $SUDO cmake --install zstd-build >/dev/null
fi

# A newly installed libbrotlidec.so.1 shares its soname with distro 1.1.0.
# Refresh the loader cache so the PREFIX build is the one that resolves.
if [[ "$(id -u)" == 0 ]]; then
  ldconfig
elif [[ -n "$SUDO" ]]; then
  $SUDO ldconfig
fi

pc_modules=()
if want nghttp2; then
  pc_modules+=(libnghttp2)
fi
if want brotli; then
  pc_modules+=(libbrotlienc libbrotlidec)
fi
if want zstd; then
  pc_modules+=(libzstd)
fi
PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" \
  pkg-config --modversion "${pc_modules[@]}"
