# Set minutely up on this machine: find a usable Python, build a private
# virtual environment beside this file, install the app into it, and leave a
# start-minutely.bat you can double-click.
#
# Run it from PowerShell in this folder:
#
#     powershell -ExecutionPolicy Bypass -File install.ps1
#
# Nothing is installed system-wide. The app itself has no dependencies, though
# the install step needs a working internet connection for a moment while pip
# fetches the packaging tool that builds it. Delete the .venv folder to undo.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Fail($message) {
    Write-Host ""
    Write-Host "X $message" -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "minutely - setting up in $PSScriptRoot"
Write-Host ""

# -- 1. find a Python we can use -------------------------------------------
# 3.11 to 3.13. Newer is not "safer" here: 3.14 is untested against this code.
$check = 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)'
$python = $null
foreach ($candidate in @("py -3.13", "py -3.12", "py -3.11", "py", "python3", "python")) {
    $parts = $candidate.Split(" ")
    $exe = $parts[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $arguments = @()
    if ($parts.Count -gt 1) { $arguments += $parts[1] }
    $arguments += @("-c", $check)
    & $exe @arguments 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
}

if (-not $python) {
    Fail "no suitable Python found (need 3.11, 3.12 or 3.13).
  Install one from https://www.python.org/downloads/ - tick 'Add Python to PATH'
  on the first screen - then run this again."
}
Write-Host "OK  using $python"

# -- 2. a private environment ----------------------------------------------
if (Test-Path ".venv") {
    Write-Host "OK  reusing the existing .venv"
} else {
    $parts = $python.Split(" ")
    $arguments = @()
    if ($parts.Count -gt 1) { $arguments += $parts[1] }
    $arguments += @("-m", "venv", ".venv")
    & $parts[0] @arguments
    if ($LASTEXITCODE -ne 0) { Fail "could not create the virtual environment." }
    Write-Host "OK  created .venv"
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$venvApp = Join-Path $PSScriptRoot ".venv\Scripts\minutely.exe"

# -- 3. install -------------------------------------------------------------
Write-Host "    installing..."
& $venvPython -m pip install --upgrade pip --quiet 2>$null
& $venvPython -m pip install -e . --quiet
if ($LASTEXITCODE -ne 0) { Fail "the install failed - the output above says why." }
$version = & $venvApp --version
Write-Host "OK  installed $version"

# -- 4. something to double-click ------------------------------------------
$launcher = Join-Path $PSScriptRoot "start-minutely.bat"
@"
@echo off
rem Starts minutely and opens it in your browser. Close this window to stop it.
cd /d "%~dp0"
".venv\Scripts\minutely.exe" record
"@ | Set-Content -Path $launcher -Encoding ASCII
Write-Host "OK  created start-minutely.bat"

# ...and an icon on the Desktop, which is what most people actually want.
# GetFolderPath rather than "$HOME\Desktop": with OneDrive backup turned on,
# the real Desktop lives under OneDrive and the literal path is wrong.
$desktop = [Environment]::GetFolderPath("Desktop")
$icon = Join-Path $PSScriptRoot "assets\minutely.ico"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $desktop "minutely.lnk"))
$shortcut.TargetPath = $venvApp
$shortcut.Arguments = "record"
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = "Record a meeting and turn it into minutes"
# Minimised: the console must stay open while the app runs, but the browser is
# the actual interface, so it belongs in the taskbar rather than in the way.
$shortcut.WindowStyle = 7
if (Test-Path $icon) { $shortcut.IconLocation = $icon }
$shortcut.Save()
Write-Host "OK  put a 'minutely' icon on your Desktop"

# -- 5. prove it works ------------------------------------------------------
Write-Host ""
Write-Host "Running the built-in sample meeting to check everything works:"
Write-Host "------------------------------------------------------------"
& $venvApp demo | Select-Object -First 24
Write-Host "------------------------------------------------------------"
Write-Host ""
Write-Host "That was a bundled example - no microphone or network involved."
Write-Host ""
Write-Host "To record a real meeting, double-click the minutely icon on your Desktop."
Write-Host ""
Read-Host "Press Enter to close"
