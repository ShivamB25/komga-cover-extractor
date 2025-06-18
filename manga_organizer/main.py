"""The main entry point for the manga organizer application.

This script initializes the application, parses arguments, and starts the
core processing logic.
"""

from . import orchestrator
from . import config

def main() -> None:
    """Main function to run the application."""
    args = config.parse_my_args()
    config.load_settings()

    if args.watchdog:
        watcher = orchestrator.Watcher()
        watcher.run()
    elif args.bookwalker_check:
        orchestrator.check_for_new_volumes_on_bookwalker()
    else:
        orchestrator.extract_covers()

if __name__ == "__main__":
    main()