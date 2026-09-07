# PowerShell installer for gemini-config on Windows.
# Links this repo into ~/.gemini (global rules) and ~/.gemini/config (global customizations).
param(
    [string]$GeminiDir = "$HOME\.gemini"
)

$ErrorActionPreference = "Stop"
$Repo = $PSScriptRoot
$Config = "$GeminiDir\config"

function Link-Dir($src, $dst) {
    if (Test-Path $dst) {
        $item = Get-Item $dst -Force
        if ($item.Attributes -match "ReparsePoint") { $item.Delete() } else { Remove-Item -Recurse -Force $dst }
    }
    New-Item -ItemType Junction -Path $dst -Target $src | Out-Null
}

function Link-File($src, $dst) {
    if (Test-Path $dst) { Remove-Item $dst -Force }
    New-Item -ItemType HardLink -Path $dst -Target $src | Out-Null
}

New-Item -ItemType Directory -Force -Path $Config | Out-Null

# 1. Global rules
Link-File "$Repo\GEMINI.md" "$GeminiDir\GEMINI.md"

# 2. Global customizations
foreach ($d in @("agents", "hooks", "scripts")) { Link-Dir "$Repo\$d" "$Config\$d" }
Link-File "$Repo\hooks.json" "$Config\hooks.json"

# 3. Skills. agy scans the global config dir only; ~/.agents/skills is workspace-scoped.
Link-Dir "$Repo\skills" "$Config\skills"

# Older installs junctioned every skill into ~/.agents/skills. Drop the links, never the targets.
$stale = "$HOME\.agents\skills"
if (Test-Path $stale) {
    Get-ChildItem $stale -Force | Where-Object { $_.Attributes -match "ReparsePoint" } | ForEach-Object {
        Write-Host "removing stale skill junction $($_.Name)"
        $_.Delete()
    }
}

# 4. MCP config: repo servers merged with machine-local mcp_config.local.json
$local = "$Config\mcp_config.local.json"
$out = "$Config\mcp_config.json"
# PSObject rather than -AsHashtable: that switch does not exist in Windows PowerShell 5.1.
$base = Get-Content "$Repo\mcp_config.json" -Raw | ConvertFrom-Json
if (Test-Path $local) {
    $extra = Get-Content $local -Raw | ConvertFrom-Json
    foreach ($k in $extra.mcpServers.PSObject.Properties.Name) {
        $base.mcpServers | Add-Member -NotePropertyName $k -NotePropertyValue $extra.mcpServers.$k -Force
    }
} else {
    Write-Warning "no $local; only repo MCP servers installed"
}
# A dead http server makes every headless run hang until its timeout, so drop it now.
foreach ($k in @($base.mcpServers.PSObject.Properties.Name)) {
    $url = $base.mcpServers.$k.serverUrl
    if (-not $url) { continue }
    try {
        Invoke-WebRequest -Uri $url -Method Head -TimeoutSec 3 -UseBasicParsing | Out-Null
    } catch {
        # any HTTP status means something answered; no response at all means dead
        if (-not $_.Exception.Response) {
            Write-Warning "MCP server '$k' at $url did not answer; leaving it out"
            $base.mcpServers.PSObject.Properties.Remove($k)
        }
    }
}

# BOM-less: Set-Content -Encoding utf8 adds one on 5.1, and agy's JSON parser rejects it.
[System.IO.File]::WriteAllText($out, ($base | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding($false)))

Write-Host "installed into $GeminiDir and $Config" -ForegroundColor Green
