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
- `apps/quality_intelligence/migrations/0002_qualityquarantine_approval_note_and_more.py`
- `apps/quality_intelligence/migrations/0003_qualityfailurereviewaudit.py`
- `apps/perf_testing/migrations/0011_alter_perfenvironment_verify_ssl.py`

设备、报告和前序统一 execution 迁移也必须随本轮功能文件一起交付。发布窗口使用目标数据库凭证执行：

```bash
python manage.py check
python manage.py makemigrations executions quality_intelligence --check --dry-run
python manage.py migrate --plan
python manage.py migrate
python scripts/ci_schema_check.py
```

本地默认 MySQL 连接如果返回 `Access denied for user 'root'@'localhost'`，只表示目标数据库的已应用迁移历史没有在本机凭证下核对。当前工作区已确认 executions、quality_intelligence 和 perf_testing 没有待生成迁移；`perf_testing` 的 `verify_ssl` 默认值已由 0011 迁移与模型对齐。部署前仍应使用目标数据库凭证复跑迁移计划和 applied history 检查。

## 重试、终态和质量门禁

`ExecutionRetryPolicy` 按项目、profile、case 解析，第一次决策会保存到 `ExecutionJob.retry_policy_snapshot`。每个 attempt 记录 retry reason、policy、mode、decision 和 scheduled time。自动重试先在 job 锁内创建 `ExecutionRetryRequest` outbox；有 dispatcher 时在事务提交后继续 dispatch，没有 dispatcher 时由后续 worker 消费 queued outbox。

`ExecutionJob.final_result`、`final_attempt`、`final_status` 和 `finalized_at` 由锁定的 finalizer 写入。晚到的 `run_finished` 或 adapter result 会保留 `ExecutionEvent`/attempt 历史，但不能覆盖 final snapshot 或 legacy report。事件幂等 key 带 job namespace，provider 在不同 job 上复用 key 不会串线。

profile 的质量门禁在 result stage 进行确定性检查；缺失指标、非法规则和非法值 fail closed，并保存 `ExecutionQualityGateEvaluation`。CI webhook 如果携带 preflight summary 会保存 webhook stage evaluation，失败时拒绝 webhook 记录而不伪造一个已执行的 terminal attempt；真实执行结果仍需经过 result stage gate。

## Quarantine 与 AI 审计

quarantine 包含 owner、expiry、approval status 和 approval note。需要审批的 quarantine 必须由独立的 privileged reviewer 批准，owner 不能自审；release 可以由 owner 或 privileged reviewer 执行。统一 dispatch 和 adapter 两个边界都会检查有效 quarantine，override 必须由 privileged actor 提供原因，并写入 project-scoped audit。

Failure Review、quarantine transition、AI suggestion review 都使用 append-only audit rows。强制重建 QI 派生 failure 时使用稳定 source/item/signature key；review audit 通过 durable source key 保留，不依赖被重建的 failure row。

## 回滚与外部边界

1. 先关闭 `EXECUTION_AI_SUGGESTIONS_ENABLED`、`QUALITY_AI_ENABLED`、`QUALITY_INTELLIGENCE_ENABLED` 和对应前端开关。
2. 停止 retry outbox worker 和 bounded QI ingestion；保留统一 execution/result rows 供调查。
3. 只在备份和目标数据库迁移计划确认后回滚新增 migration；不要删除旧的 `TestPlan`、`TestRun`、source execution 或 legacy report 数据。
4. staging APP authenticated smoke 仍需要 approved application session、fixture IDs 和 disposable fixture authorization。缺少这些输入时保持 staging 写操作关闭。
5. TLS/WSS 仍需要 approved domain/DNS、证书链、私钥交付、443/firewall 变更和独立审批；当前实现不执行 TLS/WSS rollout，也不把 HTTP/WS staging 改写成 HTTPS/WSS。

本地证据：全量 pytest `478 passed, 3 skipped`；`python -m compileall -q backend apps scripts` 通过；Django check 通过；OpenAPI warning baseline 通过；frontend lint/build 通过。