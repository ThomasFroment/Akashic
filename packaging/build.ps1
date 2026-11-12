# Builds the self-contained bundle: dist\Akashic\ (Python, every dependency, the CUDA libraries
# from the nvidia-* wheels, the UI). Copy that folder anywhere and double-click Akashic.cmd.
#   .\packaging\build.ps1            # build
#   .\packaging\build.ps1 -Zip       # also write dist\Akashic.zip
#
# Approach: a copy of uv's relocatable CPython with the locked dependencies and the akashic
# wheel installed into it (a venv is not relocatable: its pyvenv.cfg holds absolute paths).
# Models are not included; Settings downloads them only when asked (see docs/PACKAGING.md).
param([switch]$Zip)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
. "$root\scripts\env.ps1"

$dist = Join-Path $root 'dist'
$out = Join-Path $dist 'Akashic'
$work = Join-Path $dist 'work'
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
New-Item -ItemType Directory -Force $out, $work | Out-Null

Write-Host '==> wheel'
uv build --wheel --out-dir $work
if ($LASTEXITCODE) { throw 'uv build failed' }
$wheel = (Get-ChildItem $work -Filter 'akashic-*.whl' | Select-Object -First 1).FullName

Write-Host '==> python'
uv python install 3.14
# Not `uv python find`: inside the project it answers with .venv. Take the real install (a
# versioned folder, not the cpython-3.14-* link).
$pyHome = Get-ChildItem (uv python dir).Trim() -Directory |
    Where-Object { $_.Name -match '^cpython-3\.14\.\d+-windows-x86_64' -and -not $_.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint) } |
    Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $pyHome) { throw 'managed Python 3.14 not found' }
Copy-Item $pyHome (Join-Path $out 'python') -Recurse
$bundled = Join-Path $out 'python\python.exe'

Write-Host '==> dependencies (from uv.lock)'
$req = Join-Path $work 'pylock.toml'
# Preserve the exact wheel sources/hashes, including the explicit PyTorch CUDA index.
uv export --frozen --no-dev --no-emit-project --format pylock.toml -o $req | Out-Null
if ($LASTEXITCODE) { throw 'dependency export failed' }
uv pip sync --python $bundled --break-system-packages --no-cache $req
if ($LASTEXITCODE) { throw 'dependency install failed' }
uv pip install --python $bundled --break-system-packages --no-cache --no-deps $wheel
if ($LASTEXITCODE) { throw 'wheel install failed' }

Copy-Item (Join-Path $PSScriptRoot 'Akashic.cmd') $out
Copy-Item (Join-Path $root 'README.md') $out

Remove-Item $work -Recurse -Force
$size = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("==> {0} ({1:N0} MB)" -f $out, $size)
if ($Zip) { Compress-Archive -Path $out -DestinationPath (Join-Path $dist 'Akashic.zip') }
