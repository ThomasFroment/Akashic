# Runs the checks that used to be manual: real audio devices, real OCR, real GPU and Whisper.
# It plays a spoken sentence through your speakers (a few seconds).
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\selftest.ps1
Set-Location (Split-Path $PSScriptRoot -Parent)
. "$PSScriptRoot\env.ps1"
uv run pytest -m hardware --no-cov -s -q
exit $LASTEXITCODE
