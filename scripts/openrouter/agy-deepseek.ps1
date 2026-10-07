$ErrorActionPreference = "Stop"

if (-not $env:OPENROUTER_API_KEY) {
    Write-Error "OPENROUTER_API_KEY is not set in environment."
}

$Model = if ($env:OPENROUTER_MODEL) { $env:OPENROUTER_MODEL } else { "deepseek/deepseek-v4.1-flash" }
# The bridge takes the same --effort agy gets; without one it uses the model default.
$EffortArgs = @()
$i = [array]::IndexOf($args, "--effort")
if ($i -ge 0 -and $i + 1 -lt $args.Count) { $EffortArgs = @("--effort", $args[$i + 1]) }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgeScript = Join-Path $ScriptDir "openrouter_bridge.py"
$LeaseScript = Join-Path $ScriptDir "settings_lease.py"

$SettingsFile = [System.IO.Path]::Combine($HOME, ".gemini", "antigravity-cli", "settings.json")

$TmpBase = Join-Path ([System.IO.Path]::GetTempPath()) "openrouter-bridge-$PID"
$PortFile = "$TmpBase.port"
$ErrFile = "$TmpBase.err"
Remove-Item $PortFile -ErrorAction SilentlyContinue
$BridgeProc = $null
$Leased = $false
$Started = $false
$Code = 1

try {
    # Each run gets its own bridge on a free port, so parallel runs never share one.
    $BridgeProc = Start-Process python -ArgumentList (@("`"$BridgeScript`"", "--port", 0, "--model", $Model, "--port-file", "`"$PortFile`"") + $EffortArgs) -PassThru -WindowStyle Hidden -RedirectStandardError $ErrFile
    $Deadline = (Get-Date).AddSeconds(15)
    while (-not (Test-Path $PortFile)) {
        if ($BridgeProc.HasExited) { throw "bridge exited with code $($BridgeProc.ExitCode); see $ErrFile" }
        if ((Get-Date) -gt $Deadline) { throw "bridge did not start within 15 s; see $ErrFile" }
        Start-Sleep -Milliseconds 100
    }
    $Port = [int](Get-Content $PortFile -Raw)
    & python $BridgeScript --check "http://127.0.0.1:$Port" --model $Model @EffortArgs
    if ($LASTEXITCODE -ne 0) { throw "bridge health check failed; see $ErrFile" }

    & python $LeaseScript acquire $SettingsFile $PID
    if ($LASTEXITCODE -ne 0) { throw "could not patch $SettingsFile" }
    $Leased = $true

    $env:GOOGLE_GEMINI_BASE_URL = "http://127.0.0.1:$Port"
    $env:GEMINI_API_KEY = "openrouter-local-key"

    $Started = $true
    if ($args) {
        & agy @args
    } else {
        & agy
    }
    $Code = $LASTEXITCODE
} finally {
    if ($Leased) {
        & python $LeaseScript release $SettingsFile $PID
    }
    if ($BridgeProc -ne $null -and -not $BridgeProc.HasExited) {
        # /T: a venv python.exe is a shim whose child is the real server
        & taskkill /PID $BridgeProc.Id /T /F 2>&1 | Out-Null
    }
    Remove-Item $PortFile -ErrorAction SilentlyContinue
    # keep the bridge log only when start-up failed, since the error names it
    if ($Started) { Remove-Item $ErrFile -ErrorAction SilentlyContinue }
}
exit $Code
