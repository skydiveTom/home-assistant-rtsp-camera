# Recreates the throw-away .venv-ha virtual environment that ha_tests need, and
# writes the Windows only stand-ins Home Assistant cannot boot without there.
#
# Usage:
#   pwsh -File scripts/setup_ha_venv.ps1                       # create the environment
#   pwsh -File scripts/setup_ha_venv.ps1 -ShimOnly             # write the stand-ins only
#   pwsh -File scripts/setup_ha_venv.ps1 -Python python3.14    # other platforms
#
# Why the stand-ins exist: ha_tests boot a real Home Assistant core and Home
# Assistant imports the Unix only modules fcntl (homeassistant/runner.py) and
# resource (homeassistant/util/resource.py) unconditionally. On Windows those
# imports fail, so the harness cannot even start. A sitecustomize.py inside the
# virtual environment - site imports it on every start of the interpreter -
# provides them. That file is never shipped with the integration, which is the
# only reason this script stands next to the "python -m venv .venv-ha" of the
# README.

[CmdletBinding()]
param(
    # Launcher used to create the environment ("py" on Windows).
    [string]$Python = 'py',
    # Version the launcher should pick (only used by the "py" launcher).
    [string]$Version = '3.14',
    # Only (re)write the stand-in, do not touch the environment.
    [switch]$ShimOnly
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv-ha'
$sitePackages = Join-Path $venv 'Lib\site-packages'
$standIn = Join-Path $sitePackages 'sitecustomize.py'

# $IsWindows exists on PowerShell 7+ only; older versions run on Windows.
$onWindows = $null -eq $IsWindows -or $IsWindows

$standInSource = @'
"""Provide the Unix only ``fcntl`` and ``resource`` modules on Windows.

Home Assistant imports both of them unconditionally, so importing
``homeassistant.runner`` - which ``pytest-homeassistant-custom-component`` does
to boot a real core - fails on Windows without a stand-in.

``fcntl`` is used in exactly one place::

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

the single instance lock of the Home Assistant CLI, wrapped in
``except BlockingIOError``. The test harness never takes that lock, so a lock
that always succeeds is enough; the POSIX only requests raise instead of
pretending.

``resource`` is used by ``set_open_file_descriptor_limit()``, which compares the
file descriptor limit with a default and gives up when it is already reached.
Reporting the default as the current limit makes that function return before it
tries to change anything.

This file only lives in the throw-away ``.venv-ha`` virtual environment that
runs ``ha_tests``; it is never shipped with the integration. It is written by
``scripts/setup_ha_venv.ps1``, because ``site`` imports ``sitecustomize`` on
every start of the interpreter and no test has to know about it.
"""

from __future__ import annotations

import sys
import types

if sys.platform == "win32":
    _fcntl = types.ModuleType(
        "fcntl", "Stand-in for the Unix only fcntl module (see sitecustomize.py)."
    )

    #: The flags ``homeassistant.runner`` passes to :func:`flock`.
    _fcntl.LOCK_SH = 0x01  # shared lock
    _fcntl.LOCK_EX = 0x02  # exclusive lock
    _fcntl.LOCK_NB = 0x04  # do not block
    _fcntl.LOCK_UN = 0x08  # unlock

    #: The minimal set of the other constants of the real module.
    _fcntl.F_DUPFD = 0
    _fcntl.F_GETFD = 1
    _fcntl.F_SETFD = 2
    _fcntl.F_GETFL = 3
    _fcntl.F_SETFL = 4
    _fcntl.FD_CLOEXEC = 1

    def flock(fd: int, operation: int) -> None:
        """Pretend the file was locked: Windows has no ``flock()``."""
        return None

    def lockf(fd: int, cmd: int, length: int = 0, start: int = 0, whence: int = 0) -> None:
        """Pretend the file was locked: Windows has no ``lockf()``."""
        return None

    def fcntl(fd: int, cmd: int, arg: int = 0) -> int:
        """Refuse a file descriptor request that only exists on POSIX."""
        raise OSError("fcntl.fcntl() is not available on Windows")

    def ioctl(fd: int, request: int, arg: object = 0, mutate_flag: bool = True) -> int:
        """Refuse a device request that only exists on POSIX."""
        raise OSError("fcntl.ioctl() is not available on Windows")

    _fcntl.flock = flock
    _fcntl.lockf = lockf
    _fcntl.fcntl = fcntl
    _fcntl.ioctl = ioctl

    sys.modules["fcntl"] = _fcntl

    _resource = types.ModuleType(
        "resource", "Stand-in for the Unix only resource module (see sitecustomize.py)."
    )

    #: The only limit Home Assistant asks about.
    _resource.RLIMIT_NOFILE = 7  # the value of the constant on Linux
    _resource.RLIM_INFINITY = -1

    _limits = (2048, 2048)

    def getrlimit(resource_id: int) -> tuple[int, int]:
        """Return the limits of the stand-in: the defaults of Home Assistant."""
        return _limits

    def setrlimit(resource_id: int, limits: tuple[int, int]) -> None:
        """Accept a new limit without changing anything: Windows has none."""
        global _limits
        _limits = (int(limits[0]), int(limits[1]))

    _resource.getrlimit = getrlimit
    _resource.setrlimit = setrlimit

    sys.modules["resource"] = _resource
'@

function Write-StandIn {
    if (-not $onWindows) {
        Write-Host 'Not Windows: Home Assistant needs no fcntl/resource stand-in.'
        return
    }
    if (-not (Test-Path -LiteralPath $sitePackages)) {
        throw "The virtual environment is missing: $venv - run this script without -ShimOnly."
    }
    Set-Content -LiteralPath $standIn -Value $standInSource -Encoding utf8
    Write-Host "Wrote $standIn"
}

if ($ShimOnly) {
    Write-StandIn
    exit 0
}

if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "Python launcher not found: $Python (pass it with -Python)"
}

$versionArguments = @()
if ($Python -eq 'py' -and $Version) {
    $versionArguments = @("-$Version")
}

Write-Host "Creating $venv"
& $Python @versionArguments -m venv --clear $venv
if ($LASTEXITCODE -ne 0) {
    throw "python -m venv failed with exit code $LASTEXITCODE"
}

if ($onWindows) {
    $venvPython = Join-Path $venv 'Scripts\python.exe'
} else {
    $venvPython = Join-Path $venv 'bin/python'
}

Write-Host 'Upgrading pip'
& $venvPython -m pip install --disable-pip-version-check --quiet --upgrade pip

Write-Host 'Installing pytest-homeassistant-custom-component and av'
& $venvPython -m pip install --disable-pip-version-check --quiet pytest-homeassistant-custom-component av
if ($LASTEXITCODE -ne 0) {
    throw "pip install failed with exit code $LASTEXITCODE"
}

Write-StandIn

$reported = & $venvPython -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'
Write-Host ''
Write-Host "The environment runs Python $reported. Check it with:"
Write-Host "  $venvPython -m pytest -c ha_tests/pytest.ini ha_tests -q"
