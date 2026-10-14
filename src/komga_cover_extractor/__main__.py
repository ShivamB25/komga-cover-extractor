"""Run the package command line interface with ``python -m``."""


def _run() -> int:
    """Import and invoke the CLI only when execution is requested."""
    from .cli import main

    return main()


if __name__ == "__main__":  # pragma: no cover - exercised by the installed entry point
    raise SystemExit(_run())
