"""Entry point for the frozen Windows build.

PyInstaller needs a plain script to start from. ``minutely/__main__.py`` cannot
serve: it uses a relative import, which fails when a file is run directly
rather than as part of a package.
"""

from __future__ import annotations

import sys

from minutely.cli import main

if __name__ == "__main__":
    sys.exit(main())
