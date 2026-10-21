"""Command-line entry point for the document workflow."""

from .adapters.cli import main


if __name__ == '__main__':
    raise SystemExit(main())
