"""Allow ``python -m app`` to run the CLI."""

from app.cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
