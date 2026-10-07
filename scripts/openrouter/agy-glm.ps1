$env:OPENROUTER_MODEL = "z-ai/glm-5.3-flash"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $ScriptDir "agy-deepseek.ps1") @args
