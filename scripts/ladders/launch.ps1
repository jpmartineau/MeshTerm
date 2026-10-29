# SPDX-License-Identifier: Apache-2.0
#
# Open a ladder in a new Windows Terminal window, in colour, and make sure that it started.
#
#   scripts\ladders\launch.ps1 hue_ladder --L 0.70 0.75
#   scripts\ladders\launch.ps1 -Snap seam_ladder --blur 0.08
#
# The first argument is the name of the ladder (a script in this folder, without ".py").
# The other arguments go to the ladder. With -Snap, the script also takes a screenshot of
# the window and prints the path of the PNG. Refer to README.md for the three traps that
# this script avoids.
#
# The script has no param block on purpose. A param block lets PowerShell read "--L" as a
# parameter of this script. Without one, each argument arrives in $args as it was typed.

$snap = $args -contains '-Snap'
$rest = @($args | Where-Object { $_ -ne '-Snap' } | ForEach-Object { "$_" })
if ($rest.Count -lt 1) { Write-Error 'usage: launch.ps1 [-Snap] <ladder> [arguments]'; exit 2 }
$name = $rest[0] -replace '\.py$', ''
if (-not (Test-Path (Join-Path $PSScriptRoot "$name.py"))) { Write-Error "no ladder $name.py"; exit 2 }

# The tab cannot take arguments on its command line (trap 1). It gets its environment from
# this process, so the ladder and its arguments go in environment variables.
$marker = Join-Path $env:TEMP "meshterm-ladder-$name.txt"
if (Test-Path $marker) { Remove-Item $marker -Confirm:$false }
$env:LADDER_NAME = $name
$env:LADDER_ARGS = ($rest | Select-Object -Skip 1) -join ' '
$env:LADDER_MARKER = $marker
Start-Process -FilePath wt.exe -ArgumentList @('-w', 'new', '--size', '190,52', (Join-Path $PSScriptRoot 'run.cmd'))

# A launch that fails prints nothing here. Only the marker of the ladder shows that it ran.
$started = $false
foreach ($i in 1..40) {
    if (Test-Path $marker) { $started = $true; break }
    Start-Sleep -Milliseconds 500
}
if (-not $started) { Write-Error "the ladder did not start in 20 s (no $marker)"; exit 1 }
"started: $(Get-Content $marker)"

if ($snap) {
    Start-Sleep -Seconds 2  # let the ladder finish its output
    $png = Join-Path $env:TEMP "meshterm-ladder-$name.png"
    & (Join-Path $PSScriptRoot 'snap.ps1') -Title "Ladder $name" -Out $png
}
