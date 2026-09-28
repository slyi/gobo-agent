<#
.SYNOPSIS
    Run tools\gsdev.py with any available Python 3.10+.

.DESCRIPTION
    Uses a Python 3.10+ on PATH, or the portable Python that setup.ps1 downloaded
    into .tools\python (or GSDEV_PYTHON_DIR), so the dev loop works without a
    system Python or with cmd.exe / .cmd files disabled by policy.

    This is a *plain* script (no [CmdletBinding()]/param block) on purpose: it
    forwards every argument verbatim, so options like `screenshot --out FILE` are
    not mistaken for PowerShell's common parameters (-OutVariable/-OutBuffer).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\gsdev.ps1 run --headless --duration 3
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\gsdev.ps1 screenshot --out debug/x.png
#>

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "python-env.ps1")

# UTF-8 for the child Python regardless of the console code page: Japanese paths,
# CJK selectors/values, and log output.
$env:PYTHONUTF8 = "1"
if (-not $env:PYTHONIOENCODING) { $env:PYTHONIOENCODING = "utf-8" }

$python = Get-GsdevPython
if (-not $python) {
    Write-Host '[gsdev] Python 3 not found; installing a portable Python (no admin)...'
    & (Join-Path $PSScriptRoot "get-python.ps1") | Out-Null
    $python = Get-GsdevPython
    if (-not $python) { throw 'could not obtain a Python 3.10+; run setup.ps1 (see the README)' }
}

& $python.Exe @($python.Prefix) (Join-Path $PSScriptRoot "gsdev.py") @args
exit $LASTEXITCODE
