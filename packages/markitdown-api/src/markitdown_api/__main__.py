"""Command line entry point: `markitdown-api`."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .__about__ import __version__
from ._app import create_app
from ._config import load_config
from ._observability import configure_logging

logger = logging.getLogger("markitdown_api")


def main() -> None:
    config = load_config()
    # Configure logging before anything is printed, so even the startup warnings
    # below carry a timestamp and land in the configured log file.
    configure_logging(config)

    parser = argparse.ArgumentParser(
        prog="markitdown-api",
        description="Run the MarkItDown HTTP API server.",
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--host",
        default=config.host,
        help=f"Host to bind to (default: {config.host}, from MARKITDOWN_API_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.port,
        help=f"Port to listen on (default: {config.port}, from PORT).",
    )
    parser.add_argument(
        "--reload", action="store_true", help="Reload on source changes (development)."
    )
    args = parser.parse_args()

    if not config.api_keys:
        logger.warning(
            "MARKITDOWN_API_KEYS is empty, so authentication is DISABLED — anyone "
            "who can reach this port may convert files and fetch URLs through this "
            "server. Set a key in .env before exposing it."
        )

    if args.host not in ("127.0.0.1", "localhost"):
        logger.warning(
            "Binding to %s exposes this API to other machines. It converts any file "
            "it is sent, with your user's privileges. Put it behind a reverse proxy "
            "or firewall.",
            args.host,
        )

    logger.info(
        "MarkItDown API %s starting on http://%s:%d (docs at /docs, output files in %s)",
        __version__,
        args.host,
        args.port,
        config.output_dir,
    )

    # log_config=None keeps uvicorn from replacing the handlers configured above;
    # access lines are emitted by RequestLogMiddleware, with timing and an id.
    if args.reload:
        uvicorn.run(
            "markitdown_api._app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            log_config=None,
            access_log=False,
        )
    else:
        uvicorn.run(
            create_app(config),
            host=args.host,
            port=args.port,
            log_config=None,
            access_log=False,
        )


if __name__ == "__main__":
    main()
