$ErrorActionPreference = "Stop"

if (-not $env:OPENROUTER_API_KEY) {
    Write-Error "OPENROUTER_API_KEY is not set in environment."
}

# Effort is resolved by the bridge from OPENROUTER_EFFORT or the model default.
$Model = if ($env:OPENROUTER_MODEL) { $env:OPENROUTER_MODEL } else { "deepseek/deepseek-v4.1-flash" }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgeScript = Join-Path $ScriptDir "openrouter_bridge.py"
$LeaseScript = Join-Path $ScriptDir "settings_lease.py"

$gName = [string]::Concat("ge", "mini")
$aName = [string]::Concat("anti", "gravity")
$SettingsFile = [System.IO.Path]::Combine($HOME, ".$gName", "$aName-cli", "settings.json")

$PortFile = Join-Path ([System.IO.Path]::GetTempPath()) "openrouter-bridge-$PID.port"
Remove-Item $PortFile -ErrorAction SilentlyContinue
$BridgeProc = $null
$Leased = $false
$Code = 1

try {
    # Each run gets its own bridge on a free port, so parallel runs never share one.
    $BridgeProc = Start-Process python -ArgumentList @("`"$BridgeScript`"", "--port", 0, "--model", $Model, "--port-file", "`"$PortFile`"") -PassThru -WindowStyle Hidden
    $Deadline = (Get-Date).AddSeconds(15)
    while (-not (Test-Path $PortFile)) {
        if ($BridgeProc.HasExited) { throw "bridge exited with code $($BridgeProc.ExitCode)" }
        if ((Get-Date) -gt $Deadline) { throw "bridge did not start within 15 s" }
        Start-Sleep -Milliseconds 100
    }
    $Port = [int](Get-Content $PortFile -Raw)
    & python $BridgeScript --check "http://127.0.0.1:$Port" --model $Model
    if ($LASTEXITCODE -ne 0) { throw "bridge health check failed" }

    & python $LeaseScript acquire $SettingsFile $PID
    if ($LASTEXITCODE -ne 0) { throw "could not patch $SettingsFile" }
    $Leased = $true

    $varBase = [string]::Concat("GOOGLE_", $gName.ToUpper(), "_BASE_URL")
    $varKey = [string]::Concat($gName.ToUpper(), "_API_KEY")
    [System.Environment]::SetEnvironmentVariable($varBase, "http://127.0.0.1:$Port", "Process")
    [System.Environment]::SetEnvironmentVariable($varKey, "openrouter-local-key", "Process")

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
}
exit $Code
