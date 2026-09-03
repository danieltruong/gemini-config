# PowerShell installer for gemini-config on Windows.
# Links this repo into ~/.gemini (global rules) and ~/.gemini/config (global customizations).
param(
    [string]$GeminiDir = "$HOME\.gemini",
    [string]$AgentsSkillsDir = "$HOME\.agents\skills"
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

New-Item -ItemType Directory -Force -Path $Config, $AgentsSkillsDir | Out-Null

# 1. Global rules
Link-File "$Repo\GEMINI.md" "$GeminiDir\GEMINI.md"

# 2. Global customizations
foreach ($d in @("agents", "hooks", "scripts")) { Link-Dir "$Repo\$d" "$Config\$d" }
Link-File "$Repo\hooks.json" "$Config\hooks.json"

# 3. Skills into the global root and the cross-agent ~/.agents/skills dir
Link-Dir "$Repo\skills" "$Config\skills"
foreach ($skill in Get-ChildItem -Directory "$Repo\skills") {
    Link-Dir $skill.FullName "$AgentsSkillsDir\$($skill.Name)"
}

# 4. MCP config: repo servers merged with machine-local mcp_config.local.json
$local = "$Config\mcp_config.local.json"
$out = "$Config\mcp_config.json"
$base = Get-Content "$Repo\mcp_config.json" -Raw | ConvertFrom-Json -AsHashtable
if (Test-Path $local) {
    $extra = Get-Content $local -Raw | ConvertFrom-Json -AsHashtable
    foreach ($k in $extra.mcpServers.Keys) { $base.mcpServers[$k] = $extra.mcpServers[$k] }
} else {
    Write-Warning "no $local; only repo MCP servers installed"
}
$base | ConvertTo-Json -Depth 10 | Set-Content $out -Encoding utf8

Write-Host "installed into $GeminiDir and $Config" -ForegroundColor Green
