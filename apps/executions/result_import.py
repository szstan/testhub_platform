"""Import JUnit and Allure results into the unified execution domain."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from apps.testcases.models import TestCase

from .models import (
    ExecutionArtifact,
    ExecutionAttempt,
    ExecutionBuild,
    ExecutionJob,
    ExecutionStepResult,
)
from .result_events import redact_result_payload
from .state_machine import AttemptStatus, ErrorType, ExecutionStatus, StepStatus

MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_CASES = 5000
SUPPORTED_FORMATS = {"junit", "allure"}


class ResultImportError(ValueError):
    """Raised when an external result payload cannot be imported safely."""


@dataclass(frozen=True)
class ImportedCase:
    case_key: str
    name: str
    status: str
    duration_ms: int | None = None
    error_message: str = ""
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ImportSummary:
    job: ExecutionJob
    created: bool
    total: int
    passed: int
    failed: int
    skipped: int
    matched: int
    unmatched: int
    format: str
    external_run_id: str


def _text(value: Any, limit: int = 4000) -> str:
    return str(value or "").strip()[:limit]


def _bounded_key(value: str) -> str:
    value = _text(value, 1000) or "unnamed"
    if len(value) <= 255:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{value[:238]}-{digest}"


def _status(value: Any) -> str:
    value = str(value or "").strip().lower()
    if value in {"passed", "pass", "success", "successful"}:
        return "passed"
    if value in {"skipped", "skip", "pending", "disabled"}:
        return "skipped"
    if value in {"blocked", "unknown"}:
        return "blocked"
    return "failed"


def _duration_ms(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # JUnit reports seconds; Allure start/stop reports milliseconds.
    if number < 1000:
        number *= 1000
    return max(0, min(int(number), 86_400_000))


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_junit(payload: bytes) -> list[ImportedCase]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ResultImportError("JUnit XML 无法解析") from exc

    cases: list[ImportedCase] = []
    for node in root.iter():
        if _xml_local_name(node.tag) != "testcase":
            continue
        name = _text(node.attrib.get("name"), 500) or "unnamed"
        classname = _text(node.attrib.get("classname"), 500)
        external_ref = ""
        for child in node:
            if _xml_local_name(child.tag) != "properties":
                continue
            for prop in child:
                if _xml_local_name(prop.tag) == "property" and str(prop.attrib.get("name", "")).lower() in {
                    "external_ref", "automation_key", "testcase_id", "test_id"
                }:
                    external_ref = _text(prop.attrib.get("value"), 255)
                    break
        status = "passed"
        message = ""
        for child in node:
            child_name = _xml_local_name(child.tag)
            if child_name in {"failure", "error"}:
                status = "failed"
                message = _text(child.attrib.get("message") or child.text)
                break
            if child_name == "skipped":
                status = "skipped"
        case_key = external_ref or (f"{classname}::{name}" if classname else name)
        cases.append(ImportedCase(
            case_key=_bounded_key(case_key),
            name=name,
            status=status,
            duration_ms=_duration_ms(node.attrib.get("time")),
            error_message=message,
            metadata={"classname": classname} if classname else {},
        ))
    return _limit_cases(cases)


def parse_allure(payload: bytes) -> list[ImportedCase]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultImportError("Allure JSON 无法解析") from exc
    if isinstance(value, dict):
        value = value.get("results") or value.get("items") or [value]
    if not isinstance(value, list):
        raise ResultImportError("Allure JSON 必须是结果数组或包含 results 的对象")

    cases: list[ImportedCase] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name") or item.get("fullName"), 500) or "unnamed"
        external_ref = _text(
            item.get("testCaseId") or item.get("external_ref") or item.get("automation_key"),
            255,
        )
        case_key = external_ref or _text(item.get("historyId") or item.get("fullName") or name, 1000)
        details = item.get("statusDetails") or {}
        if not isinstance(details, dict):
            details = {}
        start = item.get("start")
        stop = item.get("stop")
        duration = None
        if start is not None and stop is not None:
            try:
                duration = int(stop) - int(start)
            except (TypeError, ValueError):
                duration = None
        cases.append(ImportedCase(
            case_key=_bounded_key(case_key),
            name=name,
            status=_status(item.get("status")),
            duration_ms=max(0, min(int(duration), 86_400_000)) if duration is not None else None,
            error_message=_text(details.get("message") or details.get("trace")),
            metadata={
                "full_name": _text(item.get("fullName"), 500),
                "history_id": _text(item.get("historyId"), 255),
            },
        ))
    return _limit_cases(cases)


def _limit_cases(cases: Iterable[ImportedCase]) -> list[ImportedCase]:
    values = list(cases)
    if not values:
        raise ResultImportError("结果文件中没有测试用例")
    if len(values) > MAX_CASES:
        raise ResultImportError(f"结果文件最多支持 {MAX_CASES} 条测试用例")
    return values


def parse_results(payload: bytes, result_format: str) -> list[ImportedCase]:
    if len(payload) > MAX_IMPORT_BYTES:
        raise ResultImportError("结果文件不能超过 20MB")
    result_format = str(result_format or "").strip().lower()
    if result_format not in SUPPORTED_FORMATS:
        raise ResultImportError("format 必须是 junit 或 allure")
    if result_format == "junit":
        return parse_junit(payload)
    return parse_allure(payload)


def _job_status(cases: list[ImportedCase]) -> str:
    if any(item.status == "failed" for item in cases):
        return ExecutionStatus.FAILED
    if any(item.status in {"skipped", "blocked"} for item in cases):
        return ExecutionStatus.COMPLETED
    return ExecutionStatus.PASSED


def _import_idempotency_key(result_format: str, external_run_id: str) -> str:
    digest = hashlib.sha256(f'{result_format}:{external_run_id}'.encode('utf-8')).hexdigest()
    return f'result-import:{digest}'


def _legacy_import_idempotency_key(result_format: str, external_run_id: str) -> str:
    return f'result-import:{result_format}:{external_run_id}'


def _step_idempotency_key(import_key: str, case_key: str) -> str:
    digest = hashlib.sha256(f'{import_key}:{case_key}'.encode('utf-8')).hexdigest()
    return f'result-step:{digest}'


def import_results(*, project, payload: bytes, result_format: str, external_run_id: str,
                   requested_by=None, build_reference: str = "", filename: str = "results") -> ImportSummary:
    external_run_id = _text(external_run_id, 255)
    if not external_run_id:
        raise ResultImportError("external_run_id 不能为空")
    cases = parse_results(payload, result_format)
    result_format = str(result_format).strip().lower()
    payload_hash = hashlib.sha256(payload).hexdigest()
    idempotency_key = _import_idempotency_key(result_format, external_run_id)

    with transaction.atomic():
        legacy_key = _legacy_import_idempotency_key(result_format, external_run_id)
        legacy_job = None
        if len(legacy_key) <= 128:
            legacy_job = ExecutionJob.objects.filter(
                project=project, idempotency_key=legacy_key
            ).first()
            if legacy_job is not None:
                idempotency_key = legacy_key

        build = None
        if build_reference:
            build, _ = ExecutionBuild.objects.get_or_create(
                project=project,
                build_number=build_reference,
                defaults={
                    "name": build_reference,
                    "status": "pending",
                    "created_by": requested_by,
                },
            )
        if legacy_job is not None:
            job, created = legacy_job, False
        else:
            job, created = ExecutionJob.objects.create_idempotent(
                project=project,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                defaults={
                    "source_type": "ci",
                    "source_id": external_run_id,
                    "build": build,
                    "build_reference": build_reference,
                    "trigger": "ci",
                    "status": ExecutionStatus.PENDING,
                },
            )
        if not created:
            existing_hash = str((job.result or {}).get('source_sha256') or '')
            if existing_hash and existing_hash != payload_hash:
                raise ResultImportError('相同 external_run_id 的结果内容不一致')
            summary = job.result or {}
            return ImportSummary(
                job=job,
                created=False,
                total=int(summary.get("total", 0)),
                passed=int(summary.get("passed", 0)),
                failed=int(summary.get("failed", 0)),
                skipped=int(summary.get("skipped", 0)),
                matched=int(summary.get("matched", 0)),
                unmatched=int(summary.get("unmatched", 0)),
                format=result_format,
                external_run_id=external_run_id,
            )

        job.transition_to(ExecutionStatus.QUEUED)
        job.transition_to(ExecutionStatus.RUNNING)
        attempt, _ = job.create_attempt(worker="result-import")
        attempt.transition_to(AttemptStatus.RUNNING)

        matched = 0
        step_rows = []
        for item in cases:
            testcase_exists = TestCase.objects.filter(
                project=project,
                external_ref=item.case_key,
            ).exists()
            matched += int(testcase_exists)
            step = ExecutionStepResult.objects.create(
                attempt=attempt,
                case_key=item.case_key,
                step_key="test",
                idempotency_key=_step_idempotency_key(idempotency_key, item.case_key),
                result=redact_result_payload({
                    "name": item.name,
                    "matched_testcase": testcase_exists,
                    "metadata": item.metadata or {},
                }),
                error_message=item.error_message,
                error_type=ErrorType.ASSERTION if item.status == "failed" else "",
                duration_ms=item.duration_ms,
            )
            if item.status == "skipped":
                step.transition_to(StepStatus.SKIPPED)
            else:
                step.transition_to(StepStatus.RUNNING)
                step.transition_to(StepStatus.PASSED if item.status == "passed" else StepStatus.FAILED)
            step_rows.append(step)

        failed = sum(item.status == "failed" for item in cases)
        skipped = sum(item.status in {"skipped", "blocked"} for item in cases)
        passed = sum(item.status == "passed" for item in cases)
        final_status = _job_status(cases)
        summary = redact_result_payload({
            "format": result_format,
            "external_run_id": external_run_id,
            "filename": _text(filename, 255),
            "total": len(cases),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "matched": matched,
            "unmatched": len(cases) - matched,
            "source_sha256": payload_hash,
        })
        attempt.result = summary
        attempt.is_final = True
        attempt.save(update_fields=["result", "is_final", "updated_at"])
        attempt.transition_to(AttemptStatus.PASSED if final_status == ExecutionStatus.PASSED else (
            AttemptStatus.FAILED if final_status == ExecutionStatus.FAILED else AttemptStatus.COMPLETED
        ))
        job.result = summary
        job.final_result = summary
        job.final_attempt = attempt
        job.final_status = final_status
        job.save(update_fields=["result", "final_result", "final_attempt", "final_status", "updated_at"])
        job.transition_to(final_status)
        ExecutionArtifact.objects.create(
            job=job,
            attempt=attempt,
            idempotency_key=f"result-artifact:{job.pk}:{attempt.pk}:{payload_hash[:32]}",
            uri=f"result-import://{result_format}/{external_run_id}/{_text(filename, 255)}",
            content_hash=payload_hash,
            media_type="application/xml" if result_format == "junit" else "application/json",
            size_bytes=len(payload),
            metadata=redact_result_payload({"format": result_format, "external_run_id": external_run_id}),
        )
        if build is not None:
            build.status = "failed" if failed else "passed"
            build.save(update_fields=["status", "updated_at"])

    return ImportSummary(
        job=job,
        created=True,
        total=len(cases),
        passed=passed,
        failed=failed,
        skipped=skipped,
        matched=matched,
        unmatched=len(cases) - matched,
        format=result_format,
        external_run_id=external_run_id,
    )
