#!/usr/bin/env python3
"""Small CI client for TestHub execution jobs.

Examples:
  python scripts/testhub_ci.py trigger --base-url http://testhub --project-id 7 --source-id checkout --token "$TESTHUB_TOKEN"
  python scripts/testhub_ci.py import-results --base-url http://testhub --project-id 7 --format junit --external-run-id "$GITHUB_RUN_ID" --file test-results.xml
  python scripts/testhub_ci.py release-gate --base-url http://testhub --job-id 42 --baseline-job-id 41
  python scripts/testhub_ci.py wait --base-url http://testhub --job-id 42
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests


TERMINAL = {"cancelled", "passed", "succeeded", "completed", "failed", "blocked", "unsupported"}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--base-url", required=True, help="TestHub API origin, e.g. https://testhub.example.com")
    root.add_argument("--token", default=os.environ.get("TESTHUB_TOKEN", ""), help="JWT; defaults to TESTHUB_TOKEN")
    root.add_argument("--timeout", type=float, default=15.0)
    sub = root.add_subparsers(dest="command", required=True)

    trigger = sub.add_parser("trigger")
    trigger.add_argument("--project-id", type=int, required=True)
    trigger.add_argument("--source-type", default="ci")
    trigger.add_argument("--source-id", required=True)
    trigger.add_argument("--build-reference", default="")
    trigger.add_argument("--idempotency-key", default="")

    result = sub.add_parser("import-results")
    result.add_argument("--project-id", type=int, required=True)
    result.add_argument("--format", choices=("junit", "allure"), required=True)
    result.add_argument("--external-run-id", required=True)
    result.add_argument("--build-reference", default="")
    result.add_argument("--file", type=Path, required=True)

    gate = sub.add_parser("release-gate")
    gate.add_argument("--job-id", type=int, required=True)
    gate.add_argument("--baseline-job-id", type=int)
    gate.add_argument("--max-failed", type=int, default=0)
    gate.add_argument("--max-unmatched", type=int, default=0)

    wait = sub.add_parser("wait")
    wait.add_argument("--job-id", type=int, required=True)
    wait.add_argument("--poll-seconds", type=float, default=3.0)
    wait.add_argument("--max-wait-seconds", type=float, default=1800.0)

    return root


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def request_json(method: str, endpoint: str, *, token: str, timeout: float, extra_headers=None, **kwargs):
    request_headers = headers(token)
    request_headers.update(extra_headers or {})
    response = requests.request(method, endpoint, headers=request_headers, timeout=timeout, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text[:1000]}
    if response.status_code >= 400:
        print(json.dumps(body, ensure_ascii=False), file=sys.stderr)
        return response.status_code, body
    print(json.dumps(body, ensure_ascii=False))
    return response.status_code, body


def main() -> int:
    args = parser().parse_args()
    base_url = args.base_url
    if not args.token:
        print("TESTHUB_TOKEN or --token is required", file=sys.stderr)
        return 2

    if args.command == "trigger":
        payload = {
            "project": args.project_id,
            "source_type": args.source_type,
            "source_id": args.source_id,
            "trigger": "ci",
            "build_reference": args.build_reference,
        }
        key = args.idempotency_key or f"ci-trigger:{args.source_id}:{uuid.uuid4().hex}"
        status_code, _ = request_json(
            "POST", url(base_url, "/api/executions/jobs/"), token=args.token,
            timeout=args.timeout, json=payload, extra_headers={"Idempotency-Key": key},
        )
        return 0 if status_code < 300 else 2

    if args.command == "import-results":
        if not args.file.is_file():
            print(f"result file not found: {args.file}", file=sys.stderr)
            return 2
        with args.file.open("rb") as handle:
            response = requests.post(
                url(base_url, "/api/executions/import-results/"),
                headers=headers(args.token),
                timeout=args.timeout,
                data={
                    "project_id": args.project_id,
                    "format": args.format,
                    "external_run_id": args.external_run_id,
                    "build_reference": args.build_reference,
                },
                files={"file": (args.file.name, handle)},
            )
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text[:1000]}
        print(json.dumps(body, ensure_ascii=False))
        return 0 if response.status_code < 300 else 2

    if args.command == "release-gate":
        payload = {"max_failed": args.max_failed, "max_unmatched": args.max_unmatched}
        if args.baseline_job_id:
            payload["baseline_job_id"] = args.baseline_job_id
        status_code, body = request_json(
            "POST", url(base_url, f"/api/executions/jobs/{args.job_id}/release-gate/"),
            token=args.token, timeout=args.timeout, json=payload,
        )
        return 0 if status_code < 300 and body.get("passed") else 1 if status_code == 409 else 2

    deadline = time.monotonic() + args.max_wait_seconds
    while True:
        status_code, body = request_json(
            "GET", url(base_url, f"/api/executions/jobs/{args.job_id}/ci-status/"),
            token=args.token, timeout=args.timeout,
        )
        if status_code >= 400:
            return 2
        if body.get("terminal") or body.get("status") in TERMINAL:
            return int(body.get("exit_code") if body.get("exit_code") is not None else 1)
        if time.monotonic() >= deadline:
            print("timed out waiting for TestHub job", file=sys.stderr)
            return 2
        time.sleep(max(0.5, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
