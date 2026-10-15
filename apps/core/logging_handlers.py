"""Logging handlers that defer filesystem setup until logging is configured."""
from pathlib import Path
from logging import FileHandler


class SafeFileHandler(FileHandler):
    """Create the parent directory when the handler is constructed."""

    def __init__(self, filename, mode='a', encoding=None, delay=False, errors=None):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(filename, mode=mode, encoding=encoding, delay=delay, errors=errors)
