#!/usr/bin/env python3
"""Generate the OpenAPI schema and enforce its warning baseline."""

from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTIC = re.compile(r': (Warning|Error) \[')
BLOCKED_PATTERNS = (
    re.compile(r'operationId .* has collisions', re.IGNORECASE),
    re.compile(r'multiple names for the same choice set', re.IGNORECASE),
    re.compile(r'duplicate.*choice', re.IGNORECASE),
)
ALLOWED_PATTERNS = (
    re.compile(r'unable to resolve type hint', re.IGNORECASE),
    re.compile(r'unable to guess serializer', re.IGNORECASE),
    re.compile(r'failed to obtain model through view', re.IGNORECASE),
    re.compile(r'could not derive type of path parameter', re.IGNORECASE),
    re.compile(r'could not resolve authenticator', re.IGNORECASE),
    re.compile(r'encountered \d+ components with identical names', re.IGNORECASE),
    re.compile(r'exception raised while getting serializer', re.IGNORECASE),
)


def main():
    with tempfile.TemporaryDirectory(prefix='testhub-openapi-') as temp_dir:
        schema_path = Path(temp_dir) / 'schema.yml'
        command = [
            sys.executable,
            'manage.py',
            'spectacular',
            '--validate',
            '--file',
            str(schema_path),
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    output = result.stdout + result.stderr
    if result.returncode:
        print(output, end='')
        return result.returncode

    diagnostics = [line for line in output.splitlines() if DIAGNOSTIC.search(line)]
    blocked = [
        line for line in diagnostics
        if any(pattern.search(line) for pattern in BLOCKED_PATTERNS)
    ]
    unknown = [
        line for line in diagnostics
        if not any(pattern.search(line) for pattern in ALLOWED_PATTERNS)
    ]

    if blocked or unknown:
        print('OpenAPI warning baseline failed.')
        for line in blocked:
            print('BLOCKED:', line)
        for line in unknown:
            print('UNCLASSIFIED:', line)
        return 1

    print(f'OpenAPI warning baseline passed: {len(diagnostics)} allowed diagnostics.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
