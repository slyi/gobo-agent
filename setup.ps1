<#
.SYNOPSIS
    One-shot, admin-free setup for gobo-agent on Windows.

.DESCRIPTION
    Finds a Python 3 (or downloads a portable one), then fetches the prebuilt
    tools: the official goboscript release binary and the @scratch/* browser
    bundles. No Rust, MSVC, MSYS2, Node/npm, installer, or administrator rights
    are needed.

    This is the PowerShell equivalent of setup.cmd and the preferred entry point
    where the command processor (cmd.exe / .cmd files) is disabled by policy.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -Force -Only vendor
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Offline,
    [ValidateSet("goboscript", "vendor")]
    [string]$Only,
    [switch]$AddToPath
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
. (Join-Path $root "tools\python-env.ps1")

if (-not (Test-Path -LiteralPath (Join-Path $root "tools\bootstrap.py"))) {
    throw "tools\bootstrap.py not found; run this from the repository root"
}

$python = Get-GsdevPython
if (-not $python) {
    if ($Offline) {
        throw "Python 3.10+ not found and -Offline was given. Install Python 3.10+, " +
              "or run setup without -Offline (it can fetch a portable interpreter)."
    }
    Write-Host "[setup] Python 3.10+ not found. Downloading a portable Python (no admin)..."
    $getParams = @{}
    if ($AddToPath) { $getParams["AddToPath"] = $true }
    & (Join-Path $root "tools\get-python.ps1") @getParams
    $python = Get-GsdevPython
    if (-not $python) { throw "still no Python 3.10+ after the portable install" }
}

$bootstrap = Join-Path $root "tools\bootstrap.py"
$bootstrapArgs = @()
if ($Only) { $bootstrapArgs += @("--only", $Only) }
if ($Force) { $bootstrapArgs += "--force" }
if ($Offline) { $bootstrapArgs += "--offline" }

# UTF-8 for the child Python regardless of the console code page (Japanese paths).
$env:PYTHONUTF8 = "1"
if (-not $env:PYTHONIOENCODING) { $env:PYTHONIOENCODING = "utf-8" }

# Show the interpreter that will actually run, resolving a bare PATH name.
$shown = $python.Exe
if ($python.Exe -notmatch '[\\/]') {
    $found = Get-Command $python.Exe -ErrorAction SilentlyContinue
    if ($found) { $shown = $found.Source }
}
Write-Host ("[setup] using {0} {1}" -f $shown, ($python.Prefix -join " "))
& $python.Exe @($python.Prefix) $bootstrap @bootstrapArgs
exit $LASTEXITCODE
