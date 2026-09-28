<#
.SYNOPSIS
    Install a portable, per-user Python for gobo-agent (no admin rights).

.DESCRIPTION
    gobo-agent needs Python 3.10+. On a clean Windows box this downloads the
    official CPython "embeddable" zip into <repo>\.tools\python (override with
    GSDEV_PYTHON_DIR) and checks it can do HTTPS and import the standard-library
    modules the tools use. No installer, no registry writes, and no PATH change
    unless -AddToPath is given, so it works with no administrator rights.

    The embeddable zip is the same CPython build as the normal installer (with
    python.exe and the full standard library); it just ships as a plain zip.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\get-python.ps1
.EXAMPLE
    tools\get-python.ps1 -AddToPath      # also prepend it to your user PATH
#>
[CmdletBinding()]
param(
    [string]$Version = "3.12.10",
    [string]$Dest = "",
    [switch]$Force,
    [switch]$AddToPath
)

$ErrorActionPreference = "Stop"

# Pinned sha256 for the supported version/arch pairs. python.org publishes GPG
# signatures but no checksum file for the embed zips, so these were computed from
# the python.org FTP download.
$hashes = @{
    "3.12.10/amd64" = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
    "3.12.10/arm64" = "3065efc3d382d1cda66757ac71ade11904fa6e350f5a97eb74811acd71ba5532"
}

if (-not $Dest) {
    if ($env:GSDEV_PYTHON_DIR) {
        $Dest = $env:GSDEV_PYTHON_DIR
    } else {
        $Dest = Join-Path (Split-Path -Parent $PSScriptRoot) ".tools\python"
    }
}

$arch = ""
try { $arch = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLower() } catch { $arch = "" }
if ($arch -match "arm64") { $arch = "arm64" }
elseif ($arch -match "x64|amd64") { $arch = "amd64" }
elseif ($env:PROCESSOR_ARCHITECTURE -match "ARM64") { $arch = "arm64" }
elseif ($env:PROCESSOR_ARCHITECTURE -match "64") { $arch = "amd64" }
else { throw "unsupported architecture: $($env:PROCESSOR_ARCHITECTURE)" }

$exe = Join-Path $Dest "python.exe"

function Test-PortablePython([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        # Must be the supported minimum (3.10+) as well as import the stdlib modules.
        & $Path -c "import sys;raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)" 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
        & $Path -c "import ssl, urllib.request, ctypes, tarfile, zipfile, hashlib, hmac" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

if ((Test-Path -LiteralPath $exe) -and -not $Force) {
    if (Test-PortablePython $exe) {
        Write-Host "[python] portable Python already present: $exe"
        Write-Output $exe
        exit 0
    }
    Write-Host "[python] $exe is unusable; re-downloading"
}

$asset = "python-$Version-embed-$arch.zip"
$url = "https://www.python.org/ftp/python/$Version/$asset"
$expect = $hashes["$Version/$arch"]

Write-Host "[python] downloading $asset (no admin required)"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("gobo-python-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    $zip = Join-Path $tmp $asset
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    if (-not $expect) {
        throw "no pinned checksum for Python $Version/$arch; refusing to install an " +
              "unverified interpreter (supported: $($hashes.Keys -join ', '))"
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLower()
    if ($actual -ne $expect) {
        throw "checksum mismatch for $asset (expected $expect, got $actual)"
    }
    # Only delete a directory that is clearly ours, so a mis-set -Dest /
    # GSDEV_PYTHON_DIR cannot recursively wipe an arbitrary path.
    if (Test-Path -LiteralPath $Dest) {
        $ours = (Test-Path -LiteralPath (Join-Path $Dest "python.exe")) -or
                (Test-Path -LiteralPath (Join-Path $Dest ".gobo-python"))
        if (-not $ours) {
            throw "refusing to remove '$Dest': it is not a gobo-agent Python install"
        }
        Remove-Item -Recurse -Force -LiteralPath $Dest
    }
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    Expand-Archive -LiteralPath $zip -DestinationPath $Dest -Force
    Set-Content -LiteralPath (Join-Path $Dest ".gobo-python") `
        -Value "gobo-agent portable python $Version/$arch" -Encoding ascii
} finally {
    Remove-Item -Recurse -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
}

if (-not (Test-PortablePython $exe)) { throw "the portable Python at $exe failed a self-check" }
Write-Host "[python] installed: $exe"

if ($AddToPath) {
    $parts = @()
    if ($userPath = [Environment]::GetEnvironmentVariable("Path", "User")) {
        $parts = $userPath -split ";" | Where-Object { $_ }
    }
    if ($parts -notcontains $Dest) {
        [Environment]::SetEnvironmentVariable("Path", ($Dest + ";" + ($parts -join ";")), "User")
        Write-Host "[python] added to your user PATH (restart the terminal / VS Code to pick it up)"
    } else {
        Write-Host "[python] already on your user PATH"
    }
}

Write-Output $exe
