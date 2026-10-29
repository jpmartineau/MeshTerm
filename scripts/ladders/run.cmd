@echo off
rem SPDX-License-Identifier: Apache-2.0
rem
rem The new Windows Terminal tab runs this file, with no arguments (refer to README.md,
rem trap 1). launch.ps1 puts the ladder and its arguments in the environment.
rem Claude Code runs its commands with NO_COLOR=1, and the tab gets it too (trap 2).
set NO_COLOR=
powershell.exe -NoExit -ExecutionPolicy Bypass -NoLogo -File "%~dp0run.ps1"
