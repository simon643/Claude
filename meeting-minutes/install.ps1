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

# Right-clicking a .ps1 and choosing "Run with PowerShell" closes the window
# the instant the script ends. Without this, any unexpected error flashes past
# unread and the whole thing looks like it silently did nothing — which is
# exactly how one person ended up believing it had worked when it had not.
trap {
    Write-Host ""
    Write-Host "X something unexpected went wrong:" -ForegroundColor Red
    Write-Host "  $_" -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

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
    # Say what the machine actually has. "No suitable Python" on a machine with
    # three Pythons installed is a maddening thing to be told.
    $installed = ""
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $installed = (py -0p 2>&1 | Out-String).Trim()
    }
    $detail = if ($installed) { "What this machine has:`n$installed" } else { "No Python launcher (py) found at all." }
    Fail "no Python between 3.11 and 3.13 that I can use.

  $detail

  Python 3.14 is not supported yet - it is newer than this app has been tested
  against. Install 3.13 from https://www.python.org/downloads/release/python-3130/
  (tick 'Add Python to PATH' on the first screen), then run this again.
  Having 3.14 as well is fine; they sit side by side."
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
    if ($LASTEXITCODE -ne 0) {
        Fail "could not create the virtual environment (.venv) with $python.
  The error above says why. A common cause is antivirus blocking writes into
  the Downloads folder - try moving this folder to your Documents and re-running."
    }
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
