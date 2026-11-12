# Starts Akashic. Works even if `uv` is not on this terminal's PATH yet.
#   .\akashic.ps1
Set-Location $PSScriptRoot
. "$PSScriptRoot\scripts\env.ps1"
uv run akashic @args
