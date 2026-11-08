# Build the native libraries stario links (nghttp2, Brotli, zstd, zlib) on
# Windows with vcpkg. Used by cibuildwheel and CI; the Windows counterpart of
# build-native-deps.sh.
#
# vcpkg is checked out at one commit, so every port version is fixed:
# nghttp2 1.71.0, Brotli 1.2.1 (CVE-2026-111497), zstd 1.5.7, zlib 1.3.2.
# vcpkg verifies each source archive against the SHA-512 in its portfile.
# Bump VcpkgCommit to move them, and check the versions setup.py enforces.
#
# The triplet is static libraries on the dynamic CRT (/MD, as CPython is
# built): the extensions carry the libraries, so the wheel needs no DLLs.
#
# Needs PowerShell 7, git on PATH, and network access (github.com and the
# mirrors vcpkg downloads its tools and sources from).
#
# Usage: build-native-deps.ps1 [-Prefix C:\stario-deps] [-Triplet <triplet>]
# Prints the PKG_CONFIG and PKG_CONFIG_PATH assignments for setup.py, and
# appends them to $GITHUB_ENV under GitHub Actions.
#Requires -Version 7.0
param(
    [string]$Prefix = "C:\stario-deps",
    [string]$Triplet = ""
)
$ErrorActionPreference = "Stop"
$VcpkgCommit = "ec48c11eae9448c8c6e7739d93e06fa2433aa95f"

if (-not $Triplet) {
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
        $Triplet = "arm64-windows-static-md"
    } else {
        $Triplet = "x64-windows-static-md"
    }
}

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Exe $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
# Absolute: the printed paths must work from pip's temporary build directory.
$Prefix = (Resolve-Path -LiteralPath $Prefix).Path
$vcpkgRoot = Join-Path $Prefix "vcpkg"
$vcpkg = Join-Path $vcpkgRoot "vcpkg.exe"
$current = ""
if (Test-Path (Join-Path $vcpkgRoot ".git")) {
    $current = (& git -C $vcpkgRoot rev-parse HEAD 2>$null | Out-String).Trim()
} else {
    Invoke-Checked git @("init", "-q", $vcpkgRoot)
}
# A checkout from an earlier VcpkgCommit is moved, and its installed ports
# dropped: classic-mode `vcpkg install` keeps an installed port as it is, so
# without this a bump would leave the old versions in place.
if ($current -ne $VcpkgCommit) {
    Invoke-Checked git @("-C", $vcpkgRoot, "fetch", "-q", "--depth", "1",
        "https://github.com/microsoft/vcpkg", $VcpkgCommit)
    Invoke-Checked git @("-C", $vcpkgRoot, "checkout", "-q", "--force", "FETCH_HEAD")
    $stale = Join-Path $vcpkgRoot "installed"
    if ($current -and (Test-Path $stale)) {
        Remove-Item -Recurse -Force $stale
    }
}
if ($current -ne $VcpkgCommit -or -not (Test-Path $vcpkg)) {
    Invoke-Checked (Join-Path $vcpkgRoot "bootstrap-vcpkg.bat") @("-disableMetrics")
}

# Same triplet, release only: the extensions never link the debug builds.
$overlay = Join-Path $Prefix "triplets"
New-Item -ItemType Directory -Force -Path $overlay | Out-Null
$baseTriplet = Join-Path $vcpkgRoot "triplets\$Triplet.cmake"
if (-not (Test-Path $baseTriplet)) {
    $baseTriplet = Join-Path $vcpkgRoot "triplets\community\$Triplet.cmake"
}
if (-not (Test-Path $baseTriplet)) {
    throw "vcpkg has no triplet named $Triplet"
}
$baseTriplet = $baseTriplet.Replace("\", "/")
Set-Content -Encoding ascii -Path (Join-Path $overlay "$Triplet.cmake") -Value @(
    "include(`"$baseTriplet`")",
    "set(VCPKG_BUILD_TYPE release)"
)

Invoke-Checked $vcpkg @("install", "--triplet", $Triplet,
    "--overlay-triplets", $overlay, "--disable-metrics",
    "nghttp2", "brotli", "zstd", "zlib", "pkgconf")

$installed = Join-Path $vcpkgRoot "installed\$Triplet"
$pkgConfig = Join-Path $installed "tools\pkgconf\pkgconf.exe"
$pkgConfigPath = Join-Path $installed "lib\pkgconfig"
if (-not (Test-Path $pkgConfig)) {
    throw "pkgconf.exe not found at $pkgConfig"
}
$env:PKG_CONFIG_PATH = $pkgConfigPath
Invoke-Checked $pkgConfig @("--modversion", "libnghttp2", "libbrotlienc",
    "libbrotlidec", "libzstd", "zlib")

# PowerShell assignments, ready to paste before `pip install .` / `uv sync`.
Write-Output "`$env:PKG_CONFIG = `"$pkgConfig`""
Write-Output "`$env:PKG_CONFIG_PATH = `"$pkgConfigPath`""
if ($env:GITHUB_ENV) {
    Add-Content -Encoding utf8 -Path $env:GITHUB_ENV -Value @(
        "PKG_CONFIG=$pkgConfig",
        "PKG_CONFIG_PATH=$pkgConfigPath"
    )
}
