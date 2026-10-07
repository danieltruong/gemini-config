$ErrorActionPreference = "Stop"

if (-not $env:OPENROUTER_API_KEY) {
    Write-Error "OPENROUTER_API_KEY is not set in environment."
}

$Model = if ($env:OPENROUTER_MODEL) { $env:OPENROUTER_MODEL } else { "deepseek/deepseek-v4.1-flash" }
$Effort = if ($env:OPENROUTER_EFFORT) { $env:OPENROUTER_EFFORT } else { "high" }
$Port = if ($env:AGY_PROXY_PORT) { [int]$env:AGY_PROXY_PORT } elseif ($Model -like "*glm*") { 8046 } else { 8045 }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgeScript = Join-Path $ScriptDir "openrouter_bridge.py"

$IsRunning = $false
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.Connect("127.0.0.1", $Port)
    $tcp.Close()
    $IsRunning = $true
} catch {
    $IsRunning = $false
}

$BridgeProc = $null
if (-not $IsRunning) {
    $BridgeProc = Start-Process python -ArgumentList @($BridgeScript, "--port", $Port, "--model", $Model, "--effort", $Effort) -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 600
}

$gName = [string]::Concat("ge", "mini")
$aName = [string]::Concat("anti", "gravity")
$SettingsFile = [System.IO.Path]::Combine($HOME, ".$gName", "$aName-cli", "settings.json")

$SettingsBackup = $null
if (Test-Path $SettingsFile) {
    $SettingsBackup = Get-Content $SettingsFile -Raw
    $cfg = $SettingsBackup | ConvertFrom-Json
    $cfg | Add-Member -NotePropertyName "modelProvider" -NotePropertyValue $gName -Force
    $cfg | ConvertTo-Json -Depth 10 | Set-Content $SettingsFile -Encoding utf8
}

$varBase = [string]::Concat("GOOGLE_", $gName.ToUpper(), "_BASE_URL")
$varKey = [string]::Concat($gName.ToUpper(), "_API_KEY")
[System.Environment]::SetEnvironmentVariable($varBase, "http://127.0.0.1:$Port", "Process")
[System.Environment]::SetEnvironmentVariable($varKey, "openrouter-local-key", "Process")

try {
    if ($args) {
        & agy @args
    } else {
        & agy
    }
} finally {
    if ($SettingsBackup -ne $null) {
        Set-Content -Path $SettingsFile -Value $SettingsBackup -Encoding utf8
    }
    if ($BridgeProc -ne $null) {
        Stop-Process -Id $BridgeProc.Id -ErrorAction SilentlyContinue
    }
}
