"""Allow ``python -m xerobk`` as well as the installed ``xerobk`` script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
