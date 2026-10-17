# Quality Intelligence Rollout Runbook

This runbook introduces the derived quality-intelligence layer without changing source execution semantics. API, UI, APP, and performance applications remain the system of record.

## Release Sequence

1. Deploy the application code with `QUALITY_INTELLIGENCE_ENABLED=false`, `QUALITY_AI_ENABLED=false`, and `VITE_QUALITY_INTELLIGENCE_ENABLED=false` unless the environment explicitly enables the read-only feature. The backend and frontend flags must be enabled together.
2. Run the normal health checks:

   ```bash
   python manage.py check
   python manage.py makemigrations quality_intelligence --check --dry-run
   pytest -q apps/quality_intelligence/tests
   pytest -q apps/perf_testing/tests/test_comparison_report.py apps/perf_testing/tests/test_targets_eval.py
   npm --prefix frontend run lint
   npm --prefix frontend run build
   ```

3. Apply the quality migration during the normal database migration window. Do not run a quality migration against a database whose source-app migrations are not current.
4. Discover a bounded backfill with no writes:

   ```bash
   python manage.py ingest_quality --source API --project-id <project-id> --since <iso-start> --until <iso-end> --limit 100 --dry-run
   ```

5. Run the same bounded selection without `--dry-run`. Repeat execution is expected to reuse an unchanged quality run rather than create duplicate children.
6. Enable the read-only UI/API feature after a smoke check of `/quality-intelligence/dashboard`, `/quality-intelligence/runs`, `/quality-intelligence/failures`, `/quality-intelligence/profiles`, and `/quality-intelligence/baselines`.
7. Expand source and date ranges in separate batches. Preserve command output and failure counts for the release record.

## Backfill Controls

`ingest_quality` supports `--source`, repeated `--id`/`--ids`, `--project-id`, `--since`, `--until`, `--limit`, `--dry-run`, `--force`, and `--strict-missing`.

- Use `--dry-run` for discovery and normalization checks. It does not write `QualityRun` or child rows.
- Do not use `--force` for routine retries. It deletes and rebuilds derived children for an existing quality run.
- Use `--strict-missing` when missing source records must be reported as batch failures instead of retaining a `SOURCE_MISSING` projection.
- A failed source is isolated from other IDs in the same batch. Re-run only the failed IDs after correcting the source or environment issue.
- Never pass credentials, tokens, response bodies, local paths, or raw stack dumps as command arguments.

## Rollback

The quality layer is derived and can be disabled independently:

1. Disable `QUALITY_INTELLIGENCE_ENABLED`, `QUALITY_AI_ENABLED`, and `VITE_QUALITY_INTELLIGENCE_ENABLED` through the normal deployment configuration so both API and UI are hidden.
2. Stop any running `ingest_quality` process. Existing source runners, reports, and source APIs remain usable.
3. Preserve the quality tables for investigation unless removal is explicitly approved. To remove only derived projections, use a reviewed data-management operation rather than editing source executions.
4. To roll back the schema after all quality consumers are disabled, take a database backup and run the standard Django migration rollback for this app:

   ```bash
   python manage.py migrate quality_intelligence zero
   ```

   Verify that source-app migrations and tables are unchanged. Re-apply the quality migration before restoring the feature.

5. If the migration rollback cannot be performed in the target environment, leave the feature disabled. Do not modify source tables to compensate.

## Privacy and Access Checks

- Quality rows are readable only when the caller can access the source project as owner or member; unauthorized object details are returned as `404`.
- Public payloads are bounded and redacted. Evidence paths, secrets, raw stack traces, and source payloads must not be added to UI copy, logs, or incident tickets.
- Quarantine and flaky state are informational. They do not skip source executions, rewrite source statuses, or suppress raw performance statistics.
- Baseline comparisons are deterministic and informational. They do not create CI status checks or block releases.

## Verification and Known Limitation

Use SQLite test settings for local automated tests. The default local MySQL migration consistency check requires valid local database credentials; an `Access denied for user 'root'@'localhost'` warning means the database check was not executed, not that a migration diff was generated. Run the same migration check in the deployment environment with its normal credentials before release.

CI/CD quality gates, automatic blocking, automatic quarantine, and AI failure classification are intentionally deferred from this rollout.
