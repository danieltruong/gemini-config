# PowerShell installer for gemini-config on Windows
# Links this repo into ~/.agents and ~/.gemini
param(
    [string]$AgentsDir = "$HOME\.agents",
    [string]$GeminiDir = "$HOME\.gemini"
)

$ErrorActionPreference = "Stop"
$Repo = $PSScriptRoot

Write-Host "Installing gemini-config into $AgentsDir and $GeminiDir..." -ForegroundColor Cyan

# 1. Ensure target directories exist
if (-not (Test-Path $AgentsDir)) { New-Item -ItemType Directory -Path $AgentsDir | Out-Null }
if (-not (Test-Path $GeminiDir)) { New-Item -ItemType Directory -Path $GeminiDir | Out-Null }

# 2. Hardlink GEMINI.md and AGENTS.md
foreach ($doc in @("GEMINI.md", "AGENTS.md")) {
    $src = "$Repo\$doc"
    $dst = "$GeminiDir\$doc"
    if (Test-Path $dst) { Remove-Item $dst -Force }
    New-Item -ItemType HardLink -Path $dst -Target $src | Out-Null

    $dstAgents = "$AgentsDir\$doc"
    if (Test-Path $dstAgents) { Remove-Item $dstAgents -Force }
    New-Item -ItemType HardLink -Path $dstAgents -Target $src | Out-Null
}

# 3. Directory junctions for shared folders (agents, hooks, scripts)
foreach ($dir in @("agents", "hooks", "scripts")) {
    $src = "$Repo\$dir"
    $dst = "$AgentsDir\$dir"
    if (Test-Path $dst) {
        $item = Get-Item $dst -Force
        if ($item.LinkType -eq "Junction" -or $item.Attributes -match "ReparsePoint") {
            $item.Delete()
        } else {
            Remove-Item -Recurse -Force $dst
        }
    }
    New-Item -ItemType Junction -Path $dst -Target $src | Out-Null
}

# 4. Skills: link each skill individually into ~/.agents/skills/
$skillsTarget = "$AgentsDir\skills"
if (-not (Test-Path $skillsTarget)) { New-Item -ItemType Directory -Path $skillsTarget | Out-Null }

foreach ($skillDir in Get-ChildItem -Directory "$Repo\skills") {
    $name = $skillDir.Name
    $dst = "$skillsTarget\$name"
    if (Test-Path $dst) {
        $item = Get-Item $dst -Force
        if ($item.LinkType -eq "Junction" -or $item.Attributes -match "ReparsePoint") {
            $item.Delete()
        } else {
            Remove-Item -Recurse -Force $dst
        }
    }
    New-Item -ItemType Junction -Path $dst -Target $skillDir.FullName | Out-Null
}

# 5. Link hooks.json and mcp_config.json
foreach ($cfg in @("hooks.json", "mcp_config.json")) {
    $src = "$Repo\$cfg"
    $dst = "$AgentsDir\$cfg"
    if (Test-Path $dst) { Remove-Item $dst -Force }
    Copy-Item $src $dst -Force
}

Write-Host "gemini-config successfully installed and linked!" -ForegroundColor Green
