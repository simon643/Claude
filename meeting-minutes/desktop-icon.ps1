# Put a minutely icon on the Desktop.
#
# Right-click this file in Explorer and choose "Run with PowerShell".
#
# install.ps1 does this for you already; this exists for when you want the
# icon back after deleting it, or the install predates the icon.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$app = Join-Path $PSScriptRoot ".venv\Scripts\minutely.exe"
if (-not (Test-Path $app)) {
    Write-Host ""
    Write-Host "X minutely is not installed in this folder yet." -ForegroundColor Red
    Write-Host "  Right-click install.ps1 and choose 'Run with PowerShell' first."
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

# GetFolderPath, not `$HOME\Desktop`: on a machine with OneDrive backup turned
# on, the real Desktop lives under OneDrive and the literal path is wrong.
$desktop = [Environment]::GetFolderPath("Desktop")
$icon = Join-Path $PSScriptRoot "assets\minutely.ico"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $desktop "minutely.lnk"))
$shortcut.TargetPath = $app
$shortcut.Arguments = "record"
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = "Record a meeting and turn it into minutes"
# Minimised: the console has to stay open while the app runs, but the browser
# is the actual interface, so it belongs in the taskbar rather than in the way.
$shortcut.WindowStyle = 7
if (Test-Path $icon) { $shortcut.IconLocation = $icon }
$shortcut.Save()

Write-Host ""
Write-Host "OK  'minutely' is on your Desktop."
Write-Host "    Double-click it to start; the browser opens on the recorder."
Write-Host "    A small window sits minimised in the taskbar - close it to stop."
Write-Host ""
Read-Host "Press Enter to close"
