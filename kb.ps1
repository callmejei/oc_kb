<#
  Windows equivalent of the Makefile (same target names). Usage: .\kb.ps1 up | down | status | seed | test | ...
  Logic lives in infra/local/kbinfra.py and synthetic/; this file only dispatches.
#>
param([Parameter(Position = 0)][string]$Target = "help")
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"

function Invoke-Step([string]$Exe, [string[]]$CmdArgs) {
    & $Exe @CmdArgs
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($CmdArgs -join ' ') failed with exit code $LASTEXITCODE" }
}
function Sync { Invoke-Step uv @("sync", "--all-groups", "--locked") }
function Infra([string]$Action) { Invoke-Step uv @("run", "python", "infra/local/kbinfra.py", $Action) }

switch ($Target) {
    "sync"        { Sync }
    "fetch"       { Sync; Infra "fetch" }
    "up"          { Sync; Infra "up" }
    "down"        { Infra "down" }
    "status"      { Infra "status" }
    "models"      { Infra "models" }
    "seed"        { Invoke-Step uv @("run", "python", "-m", "synthetic", "seed") }
    "migrate"     { Invoke-Step uv @("run", "python", "-m", "kb.registry.migrate"); Invoke-Step uv @("run", "python", "-m", "kb.registry.rolesync") }
    "onboard"     { Invoke-Step uv @("run", "python", "-m", "kb.ingest.bootstrap", "--wait") }
    "ontology"    { Invoke-Step uv @("run", "python", "-m", "kb.graph.build") }
    "api"         { Invoke-Step uv @("run", "uvicorn", "kb.gateway.app:create_app", "--factory", "--host", "127.0.0.1", "--port", "8000") }
    "ui"          { Push-Location ui; try { Invoke-Step npm @("ci"); Invoke-Step npm @("run", "build") } finally { Pop-Location } }
    "e2e"         { Push-Location ui; try { Invoke-Step npx @("playwright", "install", "chromium"); Invoke-Step npx @("playwright", "test") } finally { Pop-Location } }
    "leak"        { Invoke-Step uv @("run", "pytest", "-m", "leak_llm", "tests/integration/qa/test_leak_llm.py") }
    "golden"      { Invoke-Step uv @("run", "python", "-m", "kb.evaluation.golden", "--out", "docs/eval/golden-latest.json") }
    "eval"        { Invoke-Step uv @("sync", "--locked", "--project", "infra/local/ragas"); Invoke-Step uv @("run", "python", "-m", "kb.evaluation.run") }
    "lint"        { Invoke-Step uv @("run", "ruff", "check", "."); Invoke-Step uv @("run", "ruff", "format", "--check", ".") }
    "typecheck"   { Invoke-Step uv @("run", "mypy") }
    "unit"        { Invoke-Step uv @("run", "pytest", "-m", "not integration") }
    "integration" { Invoke-Step uv @("run", "pytest", "-m", "integration") }
    "test"        {
        Invoke-Step uv @("run", "ruff", "check", ".")
        Invoke-Step uv @("run", "ruff", "format", "--check", ".")
        Invoke-Step uv @("run", "mypy")
        Invoke-Step uv @("run", "pytest")
    }
    default       { Write-Output "targets: sync fetch up down status models seed migrate onboard ontology api ui e2e leak golden eval test lint typecheck unit integration" }
}
