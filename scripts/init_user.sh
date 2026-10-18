#!/bin/bash
# Safely initialize a TestHub administrator.
# The password is read interactively or from TESTHUB_ADMIN_PASSWORD by the Python script;
# it is never embedded in this file or printed.

set -euo pipefail

PROJECT_DIR="${TESTHUB_PROJECT_DIR:-/opt/testhub_platform}"
if [ ! -d "$PROJECT_DIR" ]; then
  echo "project directory does not exist: $PROJECT_DIR" >&2
  exit 1
fi

cd "$PROJECT_DIR"
if [ -x "${PYTHON_BIN:-}" ]; then
  ADMIN_PYTHON="$PYTHON_BIN"
elif [ -x "$PROJECT_DIR/venv-prod/bin/python" ]; then
  ADMIN_PYTHON="$PROJECT_DIR/venv-prod/bin/python"
elif [ -x "$PROJECT_DIR/venv/bin/python" ]; then
  ADMIN_PYTHON="$PROJECT_DIR/venv/bin/python"
else
  echo "no approved project Python interpreter found" >&2
  exit 1
fi

exec "$ADMIN_PYTHON" scripts/create_initial_admin.py
