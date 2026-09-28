# Shared Python resolution for the PowerShell launchers. Dot-source it:
#     . (Join-Path $PSScriptRoot "python-env.ps1")
#
# Works under Windows PowerShell 5.1 and PowerShell 7+. Stdlib only.
#
# The minimum supported version is 3.10 (the tools use match/case, parenthesised
# context managers, etc.). Each candidate is *run* to confirm it is 3.10+; an older
# Python 3 is rejected so the portable-interpreter fallback still runs.

function Get-GsdevPythonDir {
    # The portable-Python directory, resolved identically here and in
    # tools\get-python.ps1 so an install into GSDEV_PYTHON_DIR is found again.
    if ($env:GSDEV_PYTHON_DIR) { return $env:GSDEV_PYTHON_DIR }
    return (Join-Path (Split-Path -Parent $PSScriptRoot) ".tools\python")
}

function Test-Python3 {
    param(
        [string]$Exe,
        [string[]]$Prefix = @()
    )
    if (-not $Exe) { return $false }
    try {
        $out = & $Exe @Prefix -c "import sys;print('%d.%d' % (sys.version_info[0], sys.version_info[1]))" 2>$null
    } catch {
        return $false
    }
    if ($LASTEXITCODE -ne 0) { return $false }
    $v = (($out | Select-Object -First 1) -as [string])
    if (-not $v) { return $false }
    $v = $v.Trim()
    try { $ver = [version]$v } catch { return $false }
    return (($ver.Major -gt 3) -or ($ver.Major -eq 3 -and $ver.Minor -ge 10))
}

function Get-GsdevPython {
    # Returns an object @{ Exe = <string>; Prefix = <string[]> } for a working
    # Python 3.10+, or $null. Each candidate is *run*, not just looked up: the
    # Microsoft Store "python" App Execution Alias is on PATH even when Python is
    # not installed, and invoking it only opens the Store page.
    if ($env:GSDEV_PYTHON) {
        if (Test-Python3 $env:GSDEV_PYTHON) {
            return [pscustomobject]@{ Exe = $env:GSDEV_PYTHON; Prefix = @() }
        }
        Write-Warning "GSDEV_PYTHON=$env:GSDEV_PYTHON is not a working Python 3.10+; ignoring it"
    }
    $candidates = @(
        [pscustomobject]@{ Exe = "python"; Prefix = @() },
        [pscustomobject]@{ Exe = "py"; Prefix = @("-3") }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        if (Test-Python3 $candidate.Exe $candidate.Prefix) { return $candidate }
    }
    $portable = Join-Path (Get-GsdevPythonDir) "python.exe"
    if (Test-Python3 $portable) {
        return [pscustomobject]@{ Exe = $portable; Prefix = @() }
    }
    return $null
}
