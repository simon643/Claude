#!/usr/bin/env bash
# Set minutely up on this machine: find a usable Python, build a private
# virtual environment beside this file, install the app into it, and leave a
# launcher you can double-click.
#
# Run it from a terminal in this folder:
#
#     bash install.sh
#
# Nothing is installed system-wide. The app itself has no dependencies, though
# the install step needs a working internet connection for a moment while pip
# fetches the packaging tool that builds it. Delete the .venv folder to undo.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

say() { printf '%s\n' "$*"; }
fail() { printf '\n✗ %s\n' "$*" >&2; exit 1; }

say "minutely — setting up in $HERE"
say ""

# -- 1. find a Python we can use -------------------------------------------
# 3.11 to 3.13. Newer is not "safer" here: 3.14 is untested against this code.
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)' 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    found="$(command -v python3 >/dev/null 2>&1 && python3 --version 2>&1 || echo 'none found')"
    fail "no suitable Python (need 3.11, 3.12 or 3.13; you have: $found).
  Install one from https://www.python.org/downloads/ and run this again."
fi
say "✓ using $($PYTHON --version) at $(command -v "$PYTHON")"

# -- 2. a private environment ----------------------------------------------
if [ -d .venv ]; then
    say "✓ reusing the existing .venv"
else
    "$PYTHON" -m venv .venv || fail "could not create the virtual environment.
  On Debian or Ubuntu this usually means: sudo apt install python3-venv"
    say "✓ created .venv"
fi

# -- 3. install -------------------------------------------------------------
say "  installing..."
./.venv/bin/python -m pip install --upgrade pip --quiet 2>/dev/null || true
./.venv/bin/python -m pip install -e . --quiet || fail "the install failed — the output above says why."
VERSION="$(./.venv/bin/minutely --version)"
say "✓ installed $VERSION"

# -- 4. something to double-click ------------------------------------------
case "$(uname -s)" in
    Darwin) LAUNCHER="Start minutely.command" ;;
    *)      LAUNCHER="start-minutely.sh" ;;
esac

cat > "$LAUNCHER" <<'LAUNCH'
#!/usr/bin/env bash
# Starts minutely and opens it in your browser. Close the window to stop it.
cd "$(dirname "${BASH_SOURCE[0]}")"
exec ./.venv/bin/minutely record
LAUNCH
chmod +x "$LAUNCHER"
say "✓ created \"$LAUNCHER\" — double-click it to start recording"

# -- 5. prove it works ------------------------------------------------------
say ""
say "Running the built-in sample meeting to check everything works:"
say "────────────────────────────────────────────────────────────"
./.venv/bin/minutely demo | head -n 24
say "────────────────────────────────────────────────────────────"
say ""
say "That was a bundled example — no microphone or network involved."
say ""
say "To record a real meeting:"
say "  • double-click \"$LAUNCHER\", or"
say "  • run: ./.venv/bin/minutely record"
say ""
say "Everything it saves lives in one folder you can delete:"
say "  $(./.venv/bin/minutely config --json | ./.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["data_dir"])')"
