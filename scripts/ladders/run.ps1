# SPDX-License-Identifier: Apache-2.0
#
# Runs in the new tab that launch.ps1 opens. It runs the ladder one time, and then it gives
# the command "ladder <arguments>" to run the same ladder again with other values.

Remove-Item Env:NO_COLOR -ErrorAction SilentlyContinue
$Host.UI.RawUI.WindowTitle = "Ladder $env:LADDER_NAME"  # snap.ps1 finds the window by it

$global:LadderPython = Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'
if (-not (Test-Path $global:LadderPython)) { $global:LadderPython = 'python' }
$global:LadderScript = Join-Path $PSScriptRoot "$env:LADDER_NAME.py"

# Start-Process -NoNewWindow gives Python the console itself, and not a PowerShell pipe
# (trap 3).
function global:ladder {
    $line = (@("`"$global:LadderScript`"") + @($args | ForEach-Object { "$_" })) -join ' '
    Start-Process -FilePath $global:LadderPython -ArgumentList $line -NoNewWindow -Wait
}

ladder $env:LADDER_ARGS
Write-Host ''
Write-Host "Run it again with other values: ladder <arguments>   (ladder --help lists them)"
