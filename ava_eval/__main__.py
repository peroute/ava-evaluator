"""Allow `python -m ava_eval ...`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
