"""
Centralised logging configuration.

Provides a consistent log format and level across the application.
Logs to a file that rotates every 24 hours (at midnight).
Log files older than 2 days are automatically deleted.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_LOG_LEVEL_NAMES: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def parse_log_level(level_name: str) -> int:
    """Convert a case-insensitive log level name to its ``logging`` constant.

    Parameters
    ----------
    level_name : str
        One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``.

    Returns
    -------
    int
        The corresponding ``logging`` level constant.

    Raises
    ------
    ValueError
        If *level_name* is not a recognised level name.
    """
    upper = level_name.upper()
    if upper not in _LOG_LEVEL_NAMES:
        valid = ", ".join(_LOG_LEVEL_NAMES)
        raise ValueError(f"Unknown log level '{level_name}'. Valid: {valid}")
    return _LOG_LEVEL_NAMES[upper]


def _log_directory() -> Path:
    """Return the platform-appropriate log directory.

    Order of precedence:
    1. ``OPENDESK_LOG_DIR`` environment variable
    2. XDG state home (Linux) / Application Support (macOS) / AppData (Windows)
    3. ``~/.opendesk/logs`` fallback
    """
    env_dir = os.environ.get("OPENDESK_LOG_DIR")
    if env_dir:
        return Path(env_dir)

    if sys.platform.startswith("linux"):
        # XDG_DATA_HOME / XDG_STATE_HOME
        state_home = os.environ.get("XDG_STATE_HOME", "")
        if state_home:
            return Path(state_home) / "opendesk" / "logs"
        data_home = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
        return Path(data_home) / "opendesk" / "logs"

    if sys.platform.startswith("darwin"):
        return Path.home() / "Library" / "Logs" / "OpenDesk"

    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
        return Path(appdata) / "OpenDesk" / "logs"

    return Path.home() / ".opendesk" / "logs"


def setup_logging(level: int | None = None) -> None:
    """Configure the root logger with a standard format.

    The effective level is determined by (in order of precedence):

    1. *level* parameter (if not ``None``)
    2. ``OPENDESK_LOG_LEVEL`` environment variable (if set)
    3. ``logging.INFO`` (default, suitable for production)

    For development with verbose logging, set OPENDESK_LOG_LEVEL=DEBUG.

    Logs are written to a file that rotates every 24 hours (at midnight).
    Use ``journalctl`` (Linux systemd), ``Console.app`` (macOS), or
    ``tail -f ~/.local/share/opendesk/logs/opendesk.log`` to follow logs.

    To override the log directory, set the ``OPENDESK_LOG_DIR`` environment
    variable to the desired absolute path.

    Parameters
    ----------
    level : int | None
        Logging level, e.g. ``logging.INFO``, ``logging.WARNING``.
        Overrides the environment variable when not ``None``.
    """
    if level is None:
        env = os.environ.get("OPENDESK_LOG_LEVEL")
        if env is not None:
            level = parse_log_level(env)
        else:
            level = logging.INFO

    # ── Log file path ──────────────────────────────────────────────
    log_dir = _log_directory()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "opendesk.log"

    # ── File handler con rotazione giornaliera (24h) ───────────────
    # when="midnight" crea un nuovo file ogni giorno a mezzanotte.
    # backupCount=2 mantiene il file corrente + 1 backup.
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    handler = TimedRotatingFileHandler(
        str(log_file),
        when="midnight",
        interval=1,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers on repeated calls
    if not root.handlers:
        root.addHandler(handler)
        # Log a startup marker so the file is never empty
        root.info(
            "Log started — %s (%s)",
            log_dir,
            f"level={logging.getLevelName(level)}",
        )
    else:
        root.handlers[0] = handler
        root.info("Log handler replaced — now writing to %s", log_file)
