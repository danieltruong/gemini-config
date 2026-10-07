# PowerShell installer for gemini-config on Windows.
# Links this repo into ~/.gemini (global rules) and ~/.gemini/config (global customizations).
param(
    [string]$GeminiDir = "$HOME\.gemini",
    [string]$AgentsSkillsDir = "$HOME\.agents\skills"
)

$ErrorActionPreference = "Stop"
$Repo = $PSScriptRoot
$Config = "$GeminiDir\config"

# A real directory at a link path holds files made in the app: never replace it.
function Link-Dir($src, $dst) {
    if (Test-Path $dst) {
        if (-not ((Get-Item $dst -Force).Attributes -match "ReparsePoint")) { throw "$dst is a real directory; move its files into $src, delete it, rerun" }
        (Get-Item $dst -Force).Delete()
    }
    New-Item -ItemType Junction -Path $dst -Target $src | Out-Null
}

function Link-File($src, $dst) {
    if (Test-Path $dst) { Remove-Item $dst -Force }
    New-Item -ItemType HardLink -Path $dst -Target $src | Out-Null
}

New-Item -ItemType Directory -Force -Path $Config | Out-Null

# 1. Global rules. agy loads ~/.gemini/config/rules; ~/.gemini/antigravity-cli/rules is not read.
Link-File "$Repo\GEMINI.md" "$GeminiDir\GEMINI.md"
Link-Dir "$Repo\rules" "$Config\rules"
if (-not (Test-Path "$Repo\rules\local.md")) {
    Write-Warning "no rules\local.md; copy rules\local.example.md, set trigger: always_on, fill in paths"
}

# 2. Global customizations
foreach ($d in @("agents", "hooks", "scripts", "skills")) { Link-Dir "$Repo\$d" "$Config\$d" }
Link-File "$Repo\hooks.json" "$Config\hooks.json"
# OpenRouter scripts: a junction, as git checkout strands hard links; a file deleted from the repo needs no pruning.
New-Item -ItemType Directory -Force -Path "$HOME\scripts" | Out-Null
Link-Dir "$Repo\scripts\openrouter" "$HOME\scripts\openrouter"
# Older installs hard-linked each file into ~/scripts itself.
Get-ChildItem "$Repo\scripts\openrouter" -File | ForEach-Object {
    Remove-Item "$HOME\scripts\$($_.Name)", "$HOME\scripts\__pycache__\$($_.BaseName).*.pyc" -Force -ErrorAction SilentlyContinue
}

# 3. agy scans skills in the global config dir only; ~/.agents/skills is workspace-scoped.
# Older installs junctioned every skill there. Drop those links, never another tool's or a target.
if (Test-Path $AgentsSkillsDir) {
    Get-ChildItem $AgentsSkillsDir -Force | Where-Object {
        ($_.Attributes -match "ReparsePoint") -and "$(@($_.Target)[0])".StartsWith("$Repo\skills\", "OrdinalIgnoreCase")
    } | ForEach-Object {
        Write-Host "removing stale skill junction $($_.Name)"
        $_.Delete()
    }
}

# 4. MCP config: repo servers merged with machine-local mcp_config.local.json
$local = "$Config\mcp_config.local.json"
$out = "$Config\mcp_config.json"
# PSObject rather than -AsHashtable: that switch does not exist in Windows PowerShell 5.1.
$base = Get-Content "$Repo\mcp_config.json" -Raw | ConvertFrom-Json
# Per key, like install.sh's jq merge: a local entry setting only command keeps the repo args.
function Merge-McpServers($servers, $extra) {
    foreach ($k in $extra.PSObject.Properties.Name) {
        if ($servers.$k) {
            foreach ($p in $extra.$k.PSObject.Properties) { $servers.$k | Add-Member -NotePropertyName $p.Name -NotePropertyValue $p.Value -Force }
        } else {
            $servers | Add-Member -NotePropertyName $k -NotePropertyValue $extra.$k
        }
    }
}
if (Test-Path $local) {
    Merge-McpServers $base.mcpServers (Get-Content $local -Raw | ConvertFrom-Json).mcpServers
} else {
    Write-Warning "no $local; only repo MCP servers installed"
}
# The agy MCP docs list no ~ expansion in args: entries saying ~/ or ~\ get the real home here.
function Expand-HomeArgs($servers, $homeDir) {
    foreach ($k in @($servers.PSObject.Properties.Name)) {
        $s = $servers.$k
        if ($s.args) {
            $s.args = @($s.args | ForEach-Object {
                if ($_ -is [string] -and $_ -match '^~[\\/]') { Join-Path $homeDir ($_.Substring(2) -replace "/", "\") } else { $_ }
            })
        }
    }
}
Expand-HomeArgs $base.mcpServers $HOME
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
