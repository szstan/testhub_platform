# Functional Improvement Acceptance Matrix

验证时间：2026-10-21（本地工作区）

| Area | Acceptance evidence | Result |
| --- | --- | --- |
| Unified execution/result/traceability | `python -m pytest -q` | **PASS** — 515 passed, 3 skipped；默认 pytest 收集已包含 `apps/executions/tests` |
| Retry/final result/gate/AI governance | `python -m pytest -q apps/executions/tests apps/quality_intelligence/tests` | **PASS** — 74 passed |
| Python syntax/compile | `python -m compileall -q backend apps scripts` | **PASS** |
| Django system checks | `APP_ENV=development DEBUG=True SECRET_KEY=test-secret ALLOWED_HOSTS=local python manage.py check` | **PASS** |
| Execution/QI migration drift | SQLite-backed Django `makemigrations --check --dry-run` with normal migration modules | **PASS** — 生成 `executions.0010` 后 no changes detected；目标 MySQL applied history 仍需凭证核对。 |
| Full migration drift | `python manage.py makemigrations --check --dry-run` | **BLOCKED EXTERNALLY** — 目标 MySQL `root@localhost` 无密码连接被拒绝；staging 需使用有效凭证执行 `migrate --plan`、`showmigrations` 和 `migrate`。 |
| OpenAPI/schema warning baseline | `python scripts/ci_schema_check.py` | **PASS** — 160 allowed baseline diagnostics |
| CI workflow syntax | PyYAML parse of `.github/workflows/*.yml` | **PASS** |
| Frontend lint/build | `npm run lint && npm run build` in `frontend/` | **PASS** |
| Diff hygiene | `git -c core.whitespace=cr-at-eol diff --check` | **PASS** |
| Secret/AI boundary | `EXECUTION_AI_SUGGESTIONS_ENABLED=false` by default; redacted deterministic suggestion model and append-only review audit | **PASS** by focused tests and configuration review |
| Staging APP smoke | Authenticated application session and disposable fixture authorization | **BLOCKED EXTERNALLY**; no authorized session/fixture supplied |
| TLS/WSS rollout | Approved domain/DNS, certificate chain/private key, 443/firewall and independent approval | **DEFERRED EXTERNALLY**; no approved materials or change authorization supplied |

## Delivery boundaries

The implementation is additive. Legacy TestPlan/TestRun, source execution models, reports, device APIs, and existing QI projections remain available. New migrations are explicitly unignored for review: executions `0003`–`0010`, APP `0003`, reports `0003`, perf `0011`, and QI `0002`–`0003`.

AI suggestions remain disabled by default. Approval records review state and audit history only. CI callbacks remain disabled by default. Staging writes, deployment, release, rollback, TLS, and WSS configuration were not performed.

The migration graph is clean locally, including `perf_testing` 0011. A target database with valid credentials is still required to verify applied migration history before deployment.
