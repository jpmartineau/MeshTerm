# SPDX-License-Identifier: Apache-2.0
#
# Take a screenshot of a Windows Terminal window, found by its title, and save it as a PNG.
# Claude examines this picture before it tells the user that a ladder is ready.
#
#   scripts\ladders\snap.ps1 -Title "Ladder hue_ladder" -Out $env:TEMP\ladder.png

param([string]$Title, [string]$Out)
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class LadderWindow {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string title);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
'@
[LadderWindow]::SetProcessDPIAware() | Out-Null  # physical pixels, also on a scaled display
$window = [LadderWindow]::FindWindow('CASCADIA_HOSTING_WINDOW_CLASS', $Title)
if ($window -eq [IntPtr]::Zero) { Write-Error "no Windows Terminal window has the title '$Title'"; exit 1 }
[LadderWindow]::SetForegroundWindow($window) | Out-Null
Start-Sleep -Milliseconds 400
$rect = New-Object LadderWindow+RECT
[LadderWindow]::GetWindowRect($window, [ref]$rect) | Out-Null
$size = New-Object System.Drawing.Size ($rect.R - $rect.L), ($rect.B - $rect.T)
$bitmap = New-Object System.Drawing.Bitmap $size.Width, $size.Height
[System.Drawing.Graphics]::FromImage($bitmap).CopyFromScreen($rect.L, $rect.T, 0, 0, $size)
$bitmap.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
"snapshot: $Out ($($size.Width)x$($size.Height))"
