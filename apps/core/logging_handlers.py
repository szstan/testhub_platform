"""Logging handlers that defer filesystem setup until logging is configured."""
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SafeFileHandler(RotatingFileHandler):
    """Create the parent directory and rotate application logs."""

    def __init__(self, filename, mode='a', maxBytes=10 * 1024 * 1024,
                 backupCount=5, encoding=None, delay=False, errors=None):
        parent = Path(filename).parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            parent.chmod(0o750)
        except OSError:
            pass
        super().__init__(
            filename,
            mode=mode,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay,
            errors=errors,
        )
        try:
            Path(filename).chmod(0o640)
        except OSError:
            pass
