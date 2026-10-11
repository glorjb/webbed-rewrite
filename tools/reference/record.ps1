<#
.SYNOPSIS
  Record / check / validate reference captures of the running original Webbed (read-only).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\reference\record.ps1 -Check
  powershell -ExecutionPolicy Bypass -File tools\reference\record.ps1 -Scenario M3_jump_in_place
  powershell -ExecutionPolicy Bypass -File tools\reference\record.ps1 -Validate

  -Check     attach, print diagnostics (frame counter, room, player found, body readable) and exit.
             Exit code 0 only when ready to record (in a level, player body readable).
  -Scenario  record a session: F9 start/stop, F10 marker, Ctrl+F12 finish. Validated automatically after.
  -Validate  list all captures and whether each is usable.
  Captures go to C:\Users\profe\DesktopPlaygroundLocal\captures (outside the repository).
#>
param(
    [string]$Scenario,
    [switch]$Check,
    [switch]$Validate,
    [string]$CaptureRoot = "C:\Users\profe\DesktopPlaygroundLocal\captures"
)
$ErrorActionPreference = 'Stop'
$probe = Join-Path $PSScriptRoot 'webbed_probe.py'
$validator = Join-Path $PSScriptRoot 'validate_captures.py'
$python = (Get-Command python -ErrorAction Stop).Source
$env:DP_CAPTURES = $CaptureRoot

if ($Check) {
    & $python -I $probe --out (Join-Path $env:TEMP 'webbed_probe_check') --selftest | Select-Object -First 8
    if ($LASTEXITCODE -eq 0) { Write-Host 'READY: in a level, player body readable.' -ForegroundColor Green }
    else { Write-Host 'NOT READY: see the warnings above (enter a level first).' -ForegroundColor Yellow }
    exit $LASTEXITCODE
}
if ($Validate) {
    & $python -I $validator
    exit $LASTEXITCODE
}
if (-not $Scenario) { throw 'Pass -Scenario <ID_name> (see docs\REFERENCE_TEST_PROCEDURE.md), -Check or -Validate.' }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$name = "{0}_{1}" -f $Scenario, $stamp
$out = Join-Path $CaptureRoot $name
New-Item -ItemType Directory -Force $out | Out-Null
Write-Host "Scenario $Scenario -> $out"
Write-Host 'Switch to Webbed. F9 start/stop, F10 marker, Ctrl+F12 finish.'
& $python -I $probe --out $out
Write-Host ''
& $python -I $validator $name
