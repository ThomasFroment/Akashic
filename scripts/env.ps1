# Makes `uv` available in this PowerShell session when winget installed it but the PATH of the
# current terminal does not know about it yet. Dot-source it: . scripts\env.ps1
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    # Pick up PATH changes made since this terminal started (the usual cause).
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' +
        [Environment]::GetEnvironmentVariable('Path', 'Machine')
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $candidates = @(
        (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\astral-sh.uv*" -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { $_.FullName }),
        "$env:USERPROFILE\.local\bin",
        "$env:USERPROFILE\.cargo\bin"
    )
    foreach ($dir in $candidates) {
        if ($dir -and (Test-Path (Join-Path $dir 'uv.exe'))) {
            $env:Path = "$dir;$env:Path"
            break
        }
    }
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv was not found. Install it once:  winget install --id=astral-sh.uv -e" -ForegroundColor Red
    Write-Host "then open a NEW terminal (or run this script again)." -ForegroundColor Red
    exit 1
}
