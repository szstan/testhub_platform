"""Sensitive text entry for an already-connected Airtest Android device.

This deliberately avoids Airtest's text/cmd APIs because their debug logging
can include command arguments. The local ADB argv and script are fixed; text is
sent through stdin as newline-delimited ASCII records.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess


class SecretInputError(RuntimeError):
    """Base error for a rejected or unsuccessful secret-entry request."""


class SecretInputValidationError(SecretInputError, ValueError):
    """Raised when a value cannot be entered without risking corruption."""


class SecretInputDeviceError(SecretInputError):
    """Raised when the supplied object is not a supported Airtest Android device."""


_MAX_SECRET_LENGTH = 256
_SUBPROCESS_TIMEOUT_SECONDS = 30
_SERIAL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z", re.ASCII)
_SUPPORTED_ASCII = frozenset(chr(codepoint) for codepoint in range(32, 127))
_END_RECORD = b"__PI_SECRET_INPUT_END__\n"
_ADB_REDIRECT_ENV = frozenset({"ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_PORT"})

# This script is intentionally constant. It accepts one ASCII character per
# line and never treats stdin as shell syntax. The sentinel distinguishes a
# complete input from an interrupted stream.
_REMOTE_SCRIPT = """while IFS= read -r th_char
do
    case "$th_char" in
        '__PI_SECRET_INPUT_END__')
            exit 0
            ;;
        ' ')
            input keyevent KEYCODE_SPACE >/dev/null 2>&1 || exit 71
            ;;
        ?)
            input text "$th_char" >/dev/null 2>&1 || exit 72
            ;;
        *)
            exit 73
            ;;
    esac
done
exit 74"""


def enter_sensitive_text(device, secret: str) -> None:
    """Enter supported text into the currently focused field on ``device``.

    ``device`` must be an existing Airtest Android device whose ``adb`` object
    exposes ``adb_path``, ``serialno``, ``host``, and ``port``. This helper does not focus a field,
    inspect the active IME, install or enable an IME, or verify the resulting field
    value. It returns only after the bounded local ADB process exits with code
    zero; that is not proof that the target application accepted the text.

    Only printable ASCII and U+0020 spaces are accepted; non-ASCII text fails
    closed because its input-method mapping cannot be verified. A zero exit
    status does not prove that the application accepted every character.
    Text is not placed in the local process argv, logs, or raised error
    messages. Android's ``input text`` command receives each accepted
    character as a transient remote argument; standard Android input tooling
    offers no stdin interface for that command.
    """
    payload = _encode_secret(secret)
    target = _read_target(device)
    if target is None:
        raise SecretInputDeviceError(
            "A supported existing Airtest Android device with a local ADB server is required; no text was sent"
        )

    adb_path, serial, port = target
    argv = [
        adb_path,
        "-H",
        "127.0.0.1",
        "-P",
        str(port),
        "-s",
        serial,
        "shell",
        "sh -c " + shlex.quote(_REMOTE_SCRIPT),
    ]
    env = os.environ.copy()
    for name in tuple(env):
        if name.upper() in _ADB_REDIRECT_ENV:
            env.pop(name, None)

    result = None
    failed_to_run = False
    try:
        result = subprocess.run(
            argv,
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            env=env,
        )
    except Exception:
        # Do not retain subprocess exception text: timeout/process exceptions
        # may contain stdin, command details, or tool output.
        failed_to_run = True

    if failed_to_run or result is None or result.returncode != 0:
        raise SecretInputError(
            "Secure text entry failed or timed out; the field may be partially changed. "
            "Clear it and verify device state before retrying"
        )


def _encode_secret(secret: str) -> bytes:
    if type(secret) is not str:
        raise SecretInputValidationError("secret must be a string")
    if not secret:
        raise SecretInputValidationError("secret must not be empty")
    if len(secret) > _MAX_SECRET_LENGTH:
        raise SecretInputValidationError(
            "secret exceeds the supported length; no device action was performed"
        )
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in secret):
        raise SecretInputValidationError(
            "secret must not contain line breaks or control characters; no device action was performed"
        )
    if any(char not in _SUPPORTED_ASCII for char in secret):
        raise SecretInputValidationError(
            "unsupported secret characters; only printable ASCII is supported "
            "because device-specific input/IME mapping cannot be verified; no device action was performed"
        )

    # The allowlist above makes this encoding exact and byte-for-byte ASCII.
    return b"".join(char.encode("utf-8") + b"\n" for char in secret) + _END_RECORD


def _read_target(device):
    try:
        adb = device.adb
        adb_path = adb.adb_path
        serial = adb.serialno
        host = adb.host
        port = adb.port
    except Exception:
        return None

    if type(adb_path) is not str or not adb_path or len(adb_path) > 4096:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in adb_path):
        return None
    if type(serial) is not str or not _SERIAL_RE.fullmatch(serial):
        return None
    if type(host) is not str or host not in {"localhost", "127.0.0.1"}:
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    return adb_path, serial, port
