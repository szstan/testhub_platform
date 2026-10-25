#!/usr/bin/env bash

# Validate a Linux staging release after deployment.
# Usage: ./scripts/validate_staging_release.sh [repo-root]

set -Eeuo pipefail

REPO_ROOT="${1:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BACKEND_URL="${STAGING_BACKEND_URL:-http://127.0.0.1:18002}"
FRONTEND_URL="${STAGING_FRONTEND_URL:-http://127.0.0.1:18003}"

fail() {
    echo "staging release validation failed: $*" >&2
    exit 1
}

require_file() {
    test -f "$1" || fail "missing file: $1"
}

require_http() {
    local url="$1"
    curl --fail --silent --show-error --max-time 15 "$url" >/dev/null \
        || fail "HTTP check failed: $url"
}

cd "$REPO_ROOT"
require_file "frontend/dist/index.html"

selinux_mode="$(getenforce 2>/dev/null || true)"
if [ "$selinux_mode" = "Enforcing" ]; then
    command -v restorecon >/dev/null 2>&1 \
        || fail "SELinux is enforcing but restorecon is unavailable"
    restorecon -R frontend/dist
elif command -v restorecon >/dev/null 2>&1; then
    restorecon -R frontend/dist
fi

test -r frontend/dist/index.html || fail "frontend/dist/index.html is not readable"
if id nginx >/dev/null 2>&1 && command -v runuser >/dev/null 2>&1; then
    runuser -u nginx -- test -r frontend/dist/index.html \
        || fail "nginx cannot read frontend/dist/index.html"
fi

"$PYTHON_BIN" manage.py check
migration_plan="$("$PYTHON_BIN" manage.py migrate --plan 2>&1)" || fail "migration plan failed"
printf '%s\n' "$migration_plan"
if ! grep -q 'No planned migration operations' <<<"$migration_plan"; then
    fail "pending migrations remain; apply migrations before validating this release"
fi
command -v curl >/dev/null 2>&1 || fail "curl is required for staging HTTP checks"
require_http "${BACKEND_URL}/health/"
require_http "${BACKEND_URL}/readiness/"
require_http "${BACKEND_URL}/api/docs/"
require_http "${FRONTEND_URL}/health"
require_http "${FRONTEND_URL}/"
require_http "${FRONTEND_URL}/api/docs/"

echo "staging release validation passed"
