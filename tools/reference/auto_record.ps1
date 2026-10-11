<#
.SYNOPSIS
  Automated, state-aware reference recordings of the running original Webbed, then the regression tests.

.DESCRIPTION
  Drives the game with ordinary OS keyboard/mouse input (SendInput) while the existing READ-ONLY probe
  (webbed_probe.py) supplies the game state every frame. No memory writes, no injection, no DLLs.
  Every press is confirmed through the game's own input object (oIO); failed recordings are kept in
  DesktopPlaygroundLocal\captures_failed. See docs\REFERENCE_TEST_PROCEDURE.md, "Automated recording".

  EMERGENCY STOP: F12 or Pause/Break. Switching away from the game window or moving the mouse also stops
  the run. All held keys and buttons are released on every stop.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\reference\auto_record.ps1 -SelfTest
  powershell -ExecutionPolicy Bypass -File tools\reference\auto_record.ps1 -Check
  powershell -ExecutionPolicy Bypass -File tools\reference\auto_record.ps1 -Calibrate
  powershell -ExecutionPolicy Bypass -File tools\reference\auto_record.ps1 -All
  powershell -ExecutionPolicy Bypass -File tools\reference\auto_record.ps1 -Suite M2,W1
  powershell -ExecutionPolicy Bypass -File tools\reference\auto_record.ps1 -Scenario W1_attach_hang

  -SelfTest   runner self-test against a FAKE game (no Webbed needed): mock run + runner unit tests.
  -Check      attach read-only, report readiness and bindings; sends no input.
  -Calibrate  find which key triggers each action (the spider moves, jumps and shoots a web).
  -All        suites M2 (walk), W1 (grapple shot/attach), W2 (swing/release/pull), W3 (permanent webs).
  -Suite / -Scenario   run a subset.
  -NoPrompt   do not pause for positioning between suites (only if the spot suits every scenario).
  -NoTests    skip the regression test run at the end.
  -NoMouseFailsafe     do not stop when the cursor moves unexpectedly (if the game warps the cursor).
  -Retries N  extra attempts for transient failures (default 1).
#>
param(
    [switch]$All,
    [string[]]$Suite,
    [string[]]$Scenario,
    [switch]$Check,
    [switch]$Calibrate,
    [switch]$SelfTest,
    [switch]$NoPrompt,
    [switch]$NoTests,
    [switch]$NoMouseFailsafe,
    [int]$Retries = 1,
    [string]$CaptureRoot = "C:\Users\profe\DesktopPlaygroundLocal\captures"
)
$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'auto_runner.py'
$testrun = Join-Path $PSScriptRoot 'autoref\testrun.py'
$python = (Get-Command python -ErrorAction Stop).Source

if ($SelfTest) {
    Write-Host 'Self-test against the MOCK game (no Webbed involved; mock sessions go to a temp folder).'
    & $python -I $runner --mock --all --no-tests
    $mock = $LASTEXITCODE
    & $python -I $testrun --pattern 'test_auto_runner.py'
    $unit = $LASTEXITCODE
    & $python -I $testrun --pattern 'test_analyze_grapple.py'
    $grap = $LASTEXITCODE
    if ($mock -eq 0 -and $unit -eq 0 -and $grap -eq 0) { Write-Host 'SELF-TEST OK' -ForegroundColor Green; exit 0 }
    Write-Host 'SELF-TEST FAILED (see above)' -ForegroundColor Red
    exit 1
}

$env:DP_CAPTURES = $CaptureRoot
$argsList = @('--capture-root', $CaptureRoot, '--retries', $Retries)
if ($All) { $argsList += '--all' }
if ($Suite) { $argsList += '--suite'; $argsList += ($Suite | ForEach-Object { $_ -split ',' }) }
if ($Scenario) { $argsList += '--scenario'; $argsList += ($Scenario | ForEach-Object { $_ -split ',' }) }
if ($Check) { $argsList += '--check' }
if ($Calibrate) { $argsList += '--calibrate' }
if ($NoPrompt) { $argsList += '--no-prompt' }
if ($NoTests) { $argsList += '--no-tests' }
if ($NoMouseFailsafe) { $argsList += '--no-mouse-failsafe' }
if (-not ($All -or $Suite -or $Scenario -or $Check -or $Calibrate)) {
    throw 'Pass -All, -Suite, -Scenario, -Check, -Calibrate or -SelfTest (Get-Help this script for details).'
}

& $python -I $runner @argsList
$code = $LASTEXITCODE
switch ($code) {
    0 { Write-Host 'DONE: all scenarios passed and no regression test failed.' -ForegroundColor Green }
    1 { Write-Host 'DONE WITH FAILURES: see the report path above (failed recordings are preserved).' -ForegroundColor Yellow }
    2 { Write-Host 'NOT STARTED / NOT READY: see the message above (usually: start Webbed and enter a level).' -ForegroundColor Yellow }
    3 { Write-Host 'ABORTED (emergency stop, focus loss or takeover). All inputs were released.' -ForegroundColor Yellow }
    default { Write-Host "Exit code $code" -ForegroundColor Red }
}
exit $code
