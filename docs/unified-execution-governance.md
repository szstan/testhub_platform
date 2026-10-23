# 统一执行与治理运行手册

本手册描述统一 execution、Quality Intelligence、CI profile、设备池、数据集/插件和审计 AI 的本地交付边界。旧的 API/UI/APP/性能执行模型继续作为来源系统；统一模型保存兼容投影和历史，不替换旧状态字段。

## 开关与默认行为

以下开关默认关闭或只读：

- `QUALITY_INTELLIGENCE_ENABLED=false`：隐藏 QI API，并停止 unified result projection 写入。
- `QUALITY_AI_ENABLED=false`：保留既有 QI AI 分类开关。
- `EXECUTION_AI_SUGGESTIONS_ENABLED=false`：禁止创建执行 AI suggestion；人工 review API 只处理已记录的 suggestion。
- `VITE_QUALITY_INTELLIGENCE_ENABLED`：前端 QI 菜单和页面开关，应与后端开关一同启用。
- `ExecutionProfile.callback_enabled=false`：CI webhook 不向外部系统回写。

AI suggestion 的输入、输出、prompt version、model、token/cost、target fingerprint、确定性 hash 和 review audit 会落库；redaction 发生在持久化边界。批准或拒绝是一次性状态变更，不会自动编辑生产代码、发布或修改源测试用例。

## 迁移与验证

本轮新增的迁移包括：

- `apps/executions/migrations/0008_executionattempt_is_final_and_more.py`
- `apps/executions/migrations/0009_executionaisuggestionaudit_executionretryrequest_and_more.py`
- `apps/executions/migrations/0010_executionjob_dataset_executionjob_dataset_snapshot_and_more.py`
- `apps/testcases/migrations/0003_testcase_external_ref_and_more.py`
- `apps/executions/migrations/0011_alter_executionjob_source_type_and_more.py`
- `apps/executions/migrations/0012_alter_executionqualitygateevaluation_stage.py`
- `apps/quality_intelligence/migrations/0002_qualityquarantine_approval_note_and_more.py`
- `apps/quality_intelligence/migrations/0003_qualityfailurereviewaudit.py`
- `apps/perf_testing/migrations/0011_alter_perfenvironment_verify_ssl.py`

设备、报告和前序统一 execution 迁移也必须随本轮功能文件一起交付。发布窗口使用目标数据库凭证执行：

```bash
python manage.py check
python manage.py makemigrations executions testcases quality_intelligence --check --dry-run
python manage.py migrate --plan
python manage.py migrate
python scripts/ci_schema_check.py
```

当前 P0 迁移包含 testcases 0003、executions 0011/0012；目标 staging 已应用这些迁移并通过 `migrate --plan`。本地默认 MySQL 连接如果返回 `Access denied for user 'root'@'localhost'`，只表示目标数据库的已应用迁移历史没有在本机凭证下核对。

## 重试、终态和质量门禁

`ExecutionRetryPolicy` 按项目、profile、case 解析，第一次决策会保存到 `ExecutionJob.retry_policy_snapshot`。每个 attempt 记录 retry reason、policy、mode、decision 和 scheduled time。自动重试先在 job 锁内创建 `ExecutionRetryRequest` outbox；有 dispatcher 时在事务提交后继续 dispatch，没有 dispatcher 时由后续 worker 消费 queued outbox。

`ExecutionJob.final_result`、`final_attempt`、`final_status` 和 `finalized_at` 由锁定的 finalizer 写入。晚到的 `run_finished` 或 adapter result 会保留 `ExecutionEvent`/attempt 历史，但不能覆盖 final snapshot 或 legacy report。事件幂等 key 带 job namespace，provider 在不同 job 上复用 key 不会串线。

profile 的质量门禁在 result stage 进行确定性检查；缺失指标、非法规则和非法值 fail closed，并保存 `ExecutionQualityGateEvaluation`。CI webhook 如果携带 preflight summary 会保存 webhook stage evaluation，失败时拒绝 webhook 记录而不伪造一个已执行的 terminal attempt；真实执行结果仍需经过 result stage gate。

## Release Quality Loop（P0）

P0 将 CI 结果导入、稳定测试身份、运行对比和发布质量门禁接入统一执行模型。

### 稳定测试身份

普通测试用例使用项目内唯一的 `external_ref` 作为 Git、CI 和自动化结果的稳定关联标识。创建用例时如果没有提供该字段，系统自动生成 `TC-<随机标识>`。测试标题可以修改而不影响结果关联；同一项目内重复 `external_ref` 会被拒绝。Excel 模板支持可选的“外部引用标识”列，旧模板仍可导入。

### JUnit/Allure 结果导入

结果导入接口：

```http
POST /api/executions/import-results/
Content-Type: multipart/form-data
```

必填字段：

- `project_id`：目标项目；
- `format`：`junit` 或 `allure`；
- `external_run_id`：外部运行的稳定 ID；
- `file`：JUnit XML 或 Allure JSON 文件。

可选字段：

- `build_reference`：构建号或发布引用。

结果会创建或复用一个 `ExecutionJob`，并写入 attempt、step result 和 artifact。重复使用相同的 `format + external_run_id + project` 会返回已有任务，不会重复写入。单个文件最大 20MB，最多 5000 条测试结果；未匹配 `external_ref` 的结果保留在执行历史中，并返回 `unmatched` 数量。

### 运行对比与发布门禁

当前执行任务支持：

```http
POST /api/executions/jobs/<job_id>/compare/
POST /api/executions/jobs/<job_id>/release-gate/
```

请求可以携带 `baseline_job_id`。运行对比返回新增失败、修复失败、持续失败、新增用例、移除用例和未执行用例。

发布门禁支持：

- `max_failed`：允许的失败数量；
- `max_unmatched`：允许的未匹配数量；
- baseline 与当前运行之间的新增失败默认阻断发布。

门禁结果写入 `ExecutionJob.quality_gate_result`，并以 `ExecutionQualityGateEvaluation(stage=release)` 保存审计记录。门禁通过返回 HTTP 200 和 `exit_code=0`；门禁失败返回 HTTP 409 和 `exit_code=1`。CI CLI 或 pipeline 可以据此决定发布是否继续。

前端入口位于统一任务中心的“CI 质量闭环”页面，支持导入结果、运行对比和门禁评估。

CI pipeline 可以使用仓库内的 `scripts/testhub_ci.py`，其 exit code 可直接作为发布步骤结果：

```bash
export TESTHUB_TOKEN="..."
python scripts/testhub_ci.py import-results \
  --base-url "https://testhub.example.com" \
  --project-id 7 \
  --format junit \
  --external-run-id "${GITHUB_RUN_ID}" \
  --build-reference "${GITHUB_SHA}" \
  --file test-results.xml

python scripts/testhub_ci.py release-gate \
  --base-url "https://testhub.example.com" \
  --job-id "${TESTHUB_JOB_ID}" \
  --baseline-job-id "${TESTHUB_BASELINE_JOB_ID:-}" \
  --max-failed 0 \
  --max-unmatched 0
```

`trigger` 可以幂等创建 CI job，`wait` 可以轮询 job 的终态和质量门禁；CLI 不会打印 token 或原始测试结果。

quarantine 包含 owner、expiry、approval status 和 approval note。需要审批的 quarantine 必须由独立的 privileged reviewer 批准，owner 不能自审；release 可以由 owner 或 privileged reviewer 执行。统一 dispatch 和 adapter 两个边界都会检查有效 quarantine，override 必须由 privileged actor 提供原因，并写入 project-scoped audit。

Failure Review、quarantine transition、AI suggestion review 都使用 append-only audit rows。强制重建 QI 派生 failure 时使用稳定 source/item/signature key；review audit 通过 durable source key 保留，不依赖被重建的 failure row。

## 回滚与外部边界

1. 先关闭 `EXECUTION_AI_SUGGESTIONS_ENABLED`、`QUALITY_AI_ENABLED`、`QUALITY_INTELLIGENCE_ENABLED` 和对应前端开关。
2. 停止 retry outbox worker 和 bounded QI ingestion；保留统一 execution/result rows 供调查。
3. 只在备份和目标数据库迁移计划确认后回滚新增 migration；不要删除旧的 `TestPlan`、`TestRun`、source execution 或 legacy report 数据。
4. staging APP authenticated smoke 仍需要 approved application session、fixture IDs 和 disposable fixture authorization。缺少这些输入时保持 staging 写操作关闭。
5. TLS/WSS 仍需要 approved domain/DNS、证书链、私钥交付、443/firewall 变更和独立审批；当前实现不执行 TLS/WSS rollout，也不把 HTTP/WS staging 改写成 HTTPS/WSS。

本地证据：全量 pytest `542 passed, 3 skipped`；`python -m compileall -q backend apps scripts` 通过；Django check 通过；OpenAPI contract check 通过；frontend lint/build 通过。