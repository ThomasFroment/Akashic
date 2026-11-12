# Runs every check that must pass before a commit.
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
. "$PSScriptRoot\env.ps1"

$steps = @(
    @("ruff format", "uv", "run", "ruff", "format", "--check", "."),
    @("ruff check", "uv", "run", "ruff", "check", "."),
    @("pyright", "uv", "run", "pyright"),
    @("pytest", "uv", "run", "pytest", "-q")
)

foreach ($step in $steps) {
    Write-Host "==> $($step[0])"
    $exe = $step[1]
    $exeArgs = $step[2..($step.Length - 1)]
    & $exe @exeArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $($step[0])" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}
Write-Host "All checks passed." -ForegroundColor Green
