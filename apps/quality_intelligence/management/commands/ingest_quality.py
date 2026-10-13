"""Explicit, read-only source backfill for quality intelligence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.quality_intelligence.services.ingestion import ingest_many


SOURCE_TYPES = ("API", "UI", "APP", "PERF")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CommandError(f"invalid ISO datetime: {value}") from exc


def _source_model(source_type: str) -> Any:
    if source_type == "API":
        from apps.api_testing.models import TestExecution

        return TestExecution
    if source_type == "UI":
        from apps.ui_automation.models import TestExecution

        return TestExecution
    if source_type == "APP":
        from apps.app_automation.models import AppTestExecution

        return AppTestExecution
    if source_type == "PERF":
        from apps.perf_testing.models import PerfExecution

        return PerfExecution
    raise CommandError(f"unsupported source type: {source_type}")


def _source_ids(
    source_type: str,
    *,
    project_id: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int | None,
) -> list[str]:
    model = _source_model(source_type)
    queryset = model.objects.all().order_by("pk")
    filters: dict[str, Any] = {}
    if since is not None:
        filters["created_at__gte"] = since
    if until is not None:
        filters["created_at__lte"] = until
    if filters:
        queryset = queryset.filter(**filters)
    if project_id:
        if source_type in {"API", "UI", "PERF"}:
            queryset = queryset.filter(project_id=project_id)
        else:
            queryset = queryset.filter(
                Q(test_case__project_id=project_id) | Q(test_suite__project_id=project_id)
            )
    if limit is not None:
        queryset = queryset[:limit]
    return [str(value) for value in queryset.values_list("pk", flat=True)]


class Command(BaseCommand):
    help = "Explicitly ingest terminal API/UI/APP/PERF executions into quality intelligence"

    def add_arguments(self, parser):
        parser.add_argument("--source", choices=SOURCE_TYPES, help="source type; omitted means all sources")
        parser.add_argument(
            "--id",
            "--execution-id",
            dest="execution_ids",
            action="append",
            help="one source execution ID; repeat for multiple IDs",
        )
        parser.add_argument("--ids", nargs="+", help="source execution IDs")
        parser.add_argument("--project-id", help="limit discovery to one source project")
        parser.add_argument("--since", help="created_at lower bound in ISO-8601 format")
        parser.add_argument("--until", help="created_at upper bound in ISO-8601 format")
        parser.add_argument("--limit", type=int, default=None, help="maximum discovered IDs per source")
        parser.add_argument("--dry-run", action="store_true", help="read and normalize without writing quality data")
        parser.add_argument("--force", action="store_true", help="rebuild an unchanged projection")
        parser.add_argument(
            "--strict-missing",
            action="store_true",
            help="report missing sources as batch errors instead of storing SOURCE_MISSING",
        )

    def handle(self, *args, **options):
        source = options.get("source")
        explicit_ids = list(options.get("execution_ids") or [])
        explicit_ids.extend(options.get("ids") or [])
        if explicit_ids and not source:
            raise CommandError("--source is required when --id/--ids is supplied")
        if options.get("limit") is not None and options["limit"] <= 0:
            raise CommandError("--limit must be greater than zero")

        sources = [source] if source else list(SOURCE_TYPES)
        since = _parse_datetime(options.get("since"))
        until = _parse_datetime(options.get("until"))
        total = 0
        failed = 0
        for source_type in sources:
            ids = explicit_ids or _source_ids(
                source_type,
                project_id=options.get("project_id"),
                since=since,
                until=until,
                limit=options.get("limit"),
            )
            if not ids:
                self.stdout.write(f"{source_type}: no source executions selected")
                continue
            batch = ingest_many(
                source_type,
                ids,
                allow_missing=not options.get("strict_missing", False),
                force=options.get("force", False),
                dry_run=options.get("dry_run", False),
            )
            total += batch.succeeded
            failed += batch.failed
            prefix = "[DRY-RUN] " if options.get("dry_run") else ""
            self.stdout.write(
                f"{prefix}{source_type}: processed={batch.succeeded}, failed={batch.failed}"
            )
            for error in batch.errors:
                self.stderr.write(
                    f"{error['source_type']}:{error['source_execution_id']} "
                    f"{error['error_type']}: {error['message']}"
                )

        self.stdout.write(f"quality ingestion complete: processed={total}, failed={failed}")
        if failed:
            raise CommandError(f"{failed} source execution(s) failed; successful records were retained")
