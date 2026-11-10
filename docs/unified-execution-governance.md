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

相关迁移包含（实际应用顺序以完整迁移图为准）：

- `apps/executions/migrations/0008_executionattempt_is_final_and_more.py`
- `apps/executions/migrations/0009_executionaisuggestionaudit_executionretryrequest_and_more.py`
- `apps/executions/migrations/0010_executionjob_dataset_executionjob_dataset_snapshot_and_more.py`
- `apps/testcases/migrations/0003_testcase_external_ref_and_more.py`
- `apps/executions/migrations/0011_alter_executionjob_source_type_and_more.py`
- `apps/executions/migrations/0012_alter_executionqualitygateevaluation_stage.py`
- `apps/quality_intelligence/migrations/0002_qualityquarantine_approval_note_and_more.py`
- `apps/quality_intelligence/migrations/0003_qualityfailurereviewaudit.py`
- `apps/perf_testing/migrations/0011_alter_perfenvironment_verify_ssl.py`
- `apps/executions/migrations/0018_scale_indexes.py`
- `apps/executions/migrations/0019_execution_delivery_leases.py`
- `apps/app_automation/migrations/0005_device_lease_generation.py`
- `apps/quality_intelligence/migrations/0006_scale_indexes.py`
- `apps/quality_intelligence/migrations/0007_project_namespaces_and_cluster_index.py`

设备、报告和前序统一 execution 迁移也必须随本轮功能文件一起交付。发布窗口使用目标数据库凭证执行：

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan
python manage.py migrate
python scripts/ci_schema_check.py
```

本轮修复在一次性 SQLite 数据库应用了完整 118 节点迁移图，并验证了非空 testcases、设备、执行租约和 QI 命名空间升级的数据保留。四个变更迁移还通过了离线 MySQL DDL 编译。目标 MySQL 实例的计划、DDL 执行和历史数据验证属于候选发布窗口；本轮没有连接或迁移目标数据库。模型元数据中的 API/UI/PERF 调度租约索引与既有迁移保持一致，标准全应用漂移检查通过。

## Unified execution dispatcher contract

`ExecutionJob` is an executor-neutral record. A source adapter only dispatches when `EXECUTION_DISPATCH_ROUTES` contains a valid Celery task name for its `source_type`; the default is empty and therefore fail-closed. Configure routes with either a comma-separated value such as `api=workers.api_execute,ui=workers.ui_execute` or a settings mapping.

The destination worker receives the durable contract:

- positional arguments: `job_id`, `attempt_id`;
- keyword arguments: `execution_job_id`, `source_type`, `source_id`, and `variant_id`;
- Celery task id: `execution-<job_id>-<attempt_id>` or the variant suffix.

The worker owns source-specific execution and must publish a result through the result-events contract. TestHub records the broker dispatch id and keeps project authorization, attempt state, cancellation and late-result fencing in the unified layer. A route is not considered available when it is absent or syntactically invalid, and a job without `source_id` cannot be dispatched. Source IDs use typed forms such as `api:suite:<id>`, `api:request:<id>`, `ui:suite:<id>`, `ui:case:<id>`, `app:suite:<id>`, `app:case:<id>` and `performance:scenario:<id>`. Every source must have an enabled `ExecutionSourceBinding` for the same unified project; the binding records the legacy source project and, for APP, approved capability/lease metadata. Retry routes remain separately controlled by `EXECUTION_RETRY_DISPATCH_ROUTES` and use the retry outbox contract.

Binding 创建和 PATCH 会按有效字段验证真实 API/UI/APP/PERF 来源对象、原生项目以及调用者的 owner/member/admin 权限；worker 在执行边界重新检查活动账号和原生项目权限。统一 Project 与四种原生项目是独立命名空间，相等的数字 ID 不构成授权。APP 的批准、capabilities、lease 配置以及已批准 binding 的来源/项目范围变更或重新启用，要求特权用户操作。设备 generation `0` 是合法世代，执行记录会携带世代并进行 compare-and-release。

## 重试、终态和质量门禁

`ExecutionRetryPolicy` 按项目、profile、case 解析，第一次决策会保存到 `ExecutionJob.retry_policy_snapshot`。每个 attempt 记录 retry reason、policy、mode、decision 和 scheduled time。自动重试先在 job 锁内创建 `ExecutionRetryRequest` outbox；有 dispatcher 时在事务提交后继续 dispatch，没有 dispatcher 时由后续 worker 消费 queued outbox。

`attempt_number` 在 job 内全局唯一；矩阵的 current attempt 和重试预算分别按 variant 计算。一次手工重试只重试仍有预算的失败 variant，并为每个新 attempt 保存独立 outbox 记录。矩阵比较会读取全部 current terminal variant 的证据；最终代表 attempt 的状态可以与矩阵聚合状态不同。全成功 variant 被质量门禁阻断时，需要对应的持久失败门禁记录才能信任该聚合快照。部分缺失或非法计数不会用状态派生的零失败数掩盖。

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
python scripts/testhub_ci.py --base-url "https://testhub.example.com" import-results \
  --project-id 7 \
  --format junit \
  --external-run-id "${GITHUB_RUN_ID}" \
  --build-reference "${GITHUB_SHA}" \
  --file test-results.xml

python scripts/testhub_ci.py --base-url "https://testhub.example.com" release-gate \
  --job-id "${TESTHUB_JOB_ID}" \
  --baseline-job-id "${TESTHUB_BASELINE_JOB_ID:-}" \
  --max-failed 0 \
  --max-unmatched 0
```

`trigger` 可以幂等创建 CI job，`wait` 可以轮询 job 的终态和质量门禁；CLI 不会打印 token 或原始测试结果。

`--base-url` and `--token` are global options and belong before the subcommand; the token may also be supplied through the `TESTHUB_TOKEN` environment variable.

quarantine 包含 owner、expiry、approval status 和 approval note。需要审批的 quarantine 必须由独立的 privileged reviewer 批准，owner 不能自审；release 可以由 owner 或 privileged reviewer 执行。统一 dispatch 和 adapter 两个边界都会检查有效 quarantine，override 必须由 privileged actor 提供原因，并写入 project-scoped audit。

Failure Review、quarantine transition、AI suggestion review 都使用 append-only audit rows。强制重建 QI 派生 failure 时使用稳定 source/item/signature key；review audit 通过 durable source key 保留，不依赖被重建的 failure row。

## HTTP、证据和通知边界

私网目标开关关闭时，服务端 Requests/HTTPX 请求在 TCP 连接时重新解析并检查全部地址，再连接到校验通过的数字地址；原始 URL、Host、TLS SNI 和证书主机名验证保持一致。严格传输模式关闭环境代理，拒绝显式代理或 Unix socket 绕行。显式启用受控私网路由的配置保留原有行为。Tests-as-Code 仍执行独立的 provider host allowlist。浏览器和第三方执行器使用各自的网络运行边界，这个 HTTP 传输层不构成整台主机的出站隔离。

Evidence Explorer 的 artifact/QI page、page size 和 total 独立；`media_type` 过滤执行产物，`evidence_type` 过滤 QI 证据。每页最多 200 项，扫描 offset 上限为 100000，超限返回 4xx 并要求缩小筛选；超过真实总数的页返回空数据。生产者的截图摘要来自捕获字节，UI 结果归一化会保留引用/provenance，并对列表、映射和深度给出显式截断标记。

通知先持久化不可变去重 intent，提交后只向 broker 投递。独立 worker 使用短事务领取 120 秒 UUID 租约，在事务外访问 provider，完成时验证同一租约；最多五次投递，失败退避和过期恢复由 scheduler 驱动。provider 接受后进程崩溃仍可能导致再次投递，交付语义为 at-least-once。

## 认证恢复与验收

refresh JWT 保存在 HttpOnly cookie，access JWT 保存在内存。并发或迟到的 401 共用一次 refresh，JWT 的实际 `exp` 驱动过期判断；旧会话响应不能清除后来登录的会话。无效 refresh 返回受控 401 并移除同路径 cookie，登出先清理本地状态，再处理服务端撤销。

本地浏览器验收覆盖生产 Vue bundle 的认证和分页真实 DOM 交互，API 使用隔离合成响应。真实 staging 账号登录、APP 业务夹具和 TLS/WSS 验收仍按下面的外部门禁执行；health/readiness 结果仅说明其各自的服务状态。

## 回滚与外部边界

1. 先关闭 `EXECUTION_AI_SUGGESTIONS_ENABLED`、`QUALITY_AI_ENABLED`、`QUALITY_INTELLIGENCE_ENABLED` 和对应前端开关。
2. 停止 retry outbox worker 和 bounded QI ingestion；保留统一 execution/result rows 供调查。
3. 只在备份和目标数据库迁移计划确认后回滚新增 migration；不要删除旧的 `TestPlan`、`TestRun`、source execution 或 legacy report 数据。
4. staging APP authenticated smoke 仍需要 approved application session、fixture IDs 和 disposable fixture authorization。缺少这些输入时保持 staging 写操作关闭。
5. TLS/WSS 仍需要 approved domain/DNS、证书链、私钥交付、443/firewall 变更和独立审批；当前实现不执行 TLS/WSS rollout，也不把 HTTP/WS staging 改写成 HTTPS/WSS。

本轮本地修复证据包括完整后端、额外 API/UI/脚本回归、认证模块测试、生产 bundle 浏览器交互、编译、Django/漂移/空白检查和 OpenAPI 基线兼容检查。最终五条验收由 Goal 独立复跑并保存精确结果。OpenAPI 生成器仍报告部分文档 fallback，兼容检查通过不代表全部 API 已有完整 schema；离线 MySQL DDL 编译也不代表目标 MySQL 已执行验证。