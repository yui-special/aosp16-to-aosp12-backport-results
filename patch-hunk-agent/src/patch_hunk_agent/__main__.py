"""Allow ``python -m patch_hunk_agent``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
