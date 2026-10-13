#!/usr/bin/env python3
"""Run the focused checks required for the performance regression lane."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CHECKS = [
    [sys.executable, "manage.py", "check"],
    [sys.executable, "manage.py", "makemigrations", "--check", "--dry-run"],
    [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "apps/perf_testing/tests/test_checks.py",
        "apps/perf_testing/tests/test_comparison_report.py",
        "apps/perf_testing/tests/test_targets_eval.py",
    ],
]


for command in CHECKS:
    print("$", " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode:
        raise SystemExit(completed.returncode)
