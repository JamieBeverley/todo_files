"""Logging setup for todofiles."""

from __future__ import annotations

import logging
import sys

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def setup(level: str = "warning") -> None:
    """Configure the todofiles logger hierarchy.

    All loggers under the 'todofiles' namespace inherit from this handler.
    Output goes to stderr so it doesn't pollute command output.
    """
    numeric = _LEVELS.get(level.lower(), logging.WARNING)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-8s %(name)s: %(message)s"))
    logger = logging.getLogger("todofiles")
    logger.setLevel(numeric)
    logger.addHandler(handler)
    logger.propagate = False
