"""Logging handlers that defer filesystem setup until logging is configured."""
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SafeFileHandler(RotatingFileHandler):
    """Create the parent directory and rotate application logs."""

    def __init__(self, filename, mode='a', maxBytes=10 * 1024 * 1024,
                 backupCount=5, encoding=None, delay=False, errors=None):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(
            filename,
            mode=mode,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay,
            errors=errors,
        )
