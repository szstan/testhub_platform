# UI 自动化工作台：运行、恢复与定位器校验

工作台按“选择项目 → 准备检查 → 保存用例 → 提交执行 → 按执行编号追踪 → 查看失败步骤 → 编辑保存 → 显式复跑”组织操作。单用例与套件提交都先生成可查询的执行记录，再交给 Celery worker。受理成功表示任务已登记，最终结果以执行详情为准。

## 编辑与元素选择

- 切换用例、项目或路由时，未保存内容提供“保存并继续 / 放弃更改 / 继续编辑”。保存失败继续保留内容；保存期间新输入的内容仍保持未保存状态。
- 步骤或表单的保存请求未结算时，切换、离开与“放弃并继续”会被阻止并提示等待；即使界面已改回旧值也一样。请求结算后重新判断尚未提交的本地内容。放弃本地编辑不能撤销已发出的保存请求，不会承诺取消服务端提交。
- 创建、编辑用例弹窗的关闭按钮和取消按钮也执行同样的确认。只关闭表单时不会丢弃底层的步骤编辑。
- 浏览器刷新或关闭使用原生离开提示，包括保存请求未结算但本地界面暂时不脏的情况。该提示不等于自动保存或撤销服务端请求。
- 用例元素选择器使用 `elements/?options_only=true` 的轻量标量列表，按稳定 ID 顺序读取全部数据，每页最多 100 条；检测重复、总数变化及不完整分页，失败时提供重试。普通元素列表及详情的原有响应保持兼容。
- 显式保存空步骤数组会清空步骤；仅修改用例名称等信息不会影响步骤。步骤替换与用例信息一起保存，任一步骤写入失败时整次更新回滚。

## 受理、恢复与取消

工作台对每次显式运行生成 UUID `client_request_id`，按用户、项目、case/suite 和资源编号保存待确认身份。前端只保存 UUID、执行编号、引擎、浏览器、无头模式和原执行编号；不保存运行日志、输入快照或认证信息。

| 状态 | 工作台行为 |
| --- | --- |
| pending / PENDING | 已登记，等待 worker；继续查询同一执行编号 |
| running / RUNNING | 正在执行；周期查询详情 |
| 网络中断、响应丢失 | 显示待确认或暂停同步，查询原 UUID；显式重试也复用原 UUID |
| passed、failed、error / SUCCESS、FAILED、ERROR | 展示权威终态与证据；较旧的列表或轮询响应不能覆盖已知终态 |
| aborted / ABORTED | 已请求取消，阻止迟到结果覆盖终态 |

页面隐藏时暂停轮询，重新可见后恢复；套件报告保留已使用的轮询预算，不在隐藏期间消耗次数。项目、用户或组件上下文变化会隔离旧响应。浏览器允许 sessionStorage 时，重新进入或刷新可恢复原提交；禁止存储时，当前页面内关闭、重开复跑对话框仍复用原身份和配置，但不能保证刷新后的自动恢复。

执行页用于终态保护的缓存最多保留 100 条轻量摘要，不缓存完整日志或截图。当前详情关闭或切换后释放其大对象；若重新访问时服务器只返回旧的活动状态，界面保留已确认终态并提示详情待同步，不把旧的活动证据冒充最终证据。

取消会先写入数据库终态并撤销 claim，worker 在步骤边界检查停止信号，并关闭自己拥有的执行资源。套件子执行取消按父→子锁顺序操作，避免与 worker 保存形成反向等待。已经发出的页面动作可能已生效，复跑前应核对业务结果。worker 异常退出后不会自动重放已经开始的浏览器动作；长时间停留在 pending/running 时，应核对 broker、worker 与原执行记录，再决定取消或显式复跑。

失败执行可跳转到对应项目、用例及失败步骤，保留原执行回链。修复保存后发起的新运行通过 `rerun_of` / `rerun_kind` 关联来源；切换到其他用例会清除旧来源及步骤高亮。历史套件来源依据原执行快照，旧记录回退到其结果或精确父子执行关联，不使用当前套件成员冒充历史证据。缺少历史证据的旧记录可发起普通新运行，不伪造复跑关联。修改现有用例不会改变已受理任务的输入快照，套件快照批量读取步骤及元素策略，避免逐用例查询。

## 执行准备检查

`GET /api/ui-automation/projects/{id}/execution-readiness/` 返回项目基础 URL 的网络策略检查、可用引擎和浏览器，以及不可用原因。界面默认使用无头模式，并禁用当前部署不支持的选项。当前 UI 网络隔离限制下不能使用的 Safari/WebKit 会明确标为不可用。

准备检查不向目标页面执行测试，也不能代替 Celery worker 的运维健康监测。浏览器环境结果使用进程内短缓存：可用结果 15 秒、不可用结果 3 秒、最多 32 项；按规范化浏览器、网络配置和检查器隔离，同一检查的并发请求合并。Playwright Chrome/Edge 的当前 Chromium 等价检查可复用。项目 URL 和用户权限每次请求重新核对，实际 worker 的执行前检查不使用此缓存。

broker 派发失败会记录到原执行的 dispatch 终态，不会被当作业务断言失败。case worker 收到执行前的普通拒绝响应也会通过 claim fence 写入失败终态；已经持久化的引擎错误或取消结果不会被后续响应覆盖。

## 定位器结果的含义

元素管理页提供两种操作：

| 模式 | 实际检查 | 结果 |
| --- | --- | --- |
| 仅检查格式 | 使用 cssselect2 / lxml 解析 CSS / XPath，或检查受支持策略 | 已证实语法错误为 INVALID；语法有效仍为 UNKNOWN |
| 实时校验 | 在受 UI 出站策略约束的隔离无头 Playwright 上打开指定 URL，统计并检查匹配元素 | 可用且满足唯一性要求为 VALID；无匹配、不可用或不满足唯一性为 INVALID；浏览器、导航、依赖或策略不可用为 UNKNOWN |

实时校验不点击页面，也不执行测试步骤。页面展示校验模式、匹配数与检查时间。校验针对已保存的定位器；前端发送当前表达式和策略的预期值，发现未保存或并发修改时返回 UNKNOWN。保存新的表达式、策略、项目或唯一性设置会清除旧校验结果；并发修改后的迟到校验也不能把新表达式标成 VALID。通用元素创建/更新接口不能伪造校验状态与时间。

目标 URL 默认采用项目基础 URL。UI 自动化沿用自身的网络限制，没有性能测试私网豁免。接口返回的目标上下文会移除凭据、查询串和片段。

## API 兼容性

| 用途 | 路径或约定 |
| --- | --- |
| 单用例异步提交 | `POST test-cases/{id}/run/`，工作台发送 `async: true` 与 UUID |
| 旧单用例调用 | 不传 `async: true` 时保留同步语义 |
| 套件提交 | `POST test-suites/{id}/run_suite/`，发送 UUID |
| 恢复受理身份 | 资源下的 `GET submission/?client_request_id=UUID`，按当前用户隔离 |
| 权威详情 | `GET test-case-executions/{id}/` / `GET test-executions/{id}/` |
| 取消 | 上述执行资源下的 `POST abort/` |
| 元素选项 | `GET elements/?options_only=true`，标准分页、每页最多 100；仅返回选项所需标量字段 |
| 定位器 | `POST elements/{id}/validate_locator/`，`mode: format/live`；可带 `target_url`、`expected_locator_value`、`expected_locator_strategy_id` |

以上路径均以 `/api/ui-automation/` 为前缀。不同请求参数不能悄悄复用同一 UUID 创建另一任务。运行权限、项目关联、复跑来源与快照中的用例/元素关系都由服务端核对。claim、取消标识与私有输入快照不由客户端直接控制。工作台执行记录及其套件子执行由 worker 维护，普通 PATCH 会明确拒绝；停止操作使用专用取消接口。无 worker claim 的历史记录保留原有更新兼容性。幂等重放使用已受理快照及计数，不因之后清空或调整套件内容而变成新的预检失败。

## 部署要求

本次代码没有自动部署或应用生产迁移。上线时先安装更新后的后端依赖，再执行迁移并更新 Web 与 worker 进程。复审修复仅调整执行/读取/缓存逻辑，不增加 0007 之后的新迁移：

```bash
python -m pip install -r requirements-prod.txt
python manage.py migrate ui_automation
celery -A backend worker -l info -Q celery
```

Windows 本地调试可给 worker 加 `--pool=solo`。两个新任务 `apps.ui_automation.tasks.execute_workbench_case` 和 `apps.ui_automation.tasks.execute_workbench_suite` 使用默认 `celery` 队列，需有可用 broker 和消费该队列的 worker。迁移 `0007_workbench_async_execution` 依赖 `0006_restore_case_failure_steps`，增加提交身份、快照、claim、取消与来源字段，并使用 MySQL 支持的普通复合唯一约束。

CSS 格式校验新增直接依赖 `cssselect2==0.8.0`；XPath 使用现有 `lxml`。缺少浏览器二进制时，准备检查和实时校验会报告不可用，不伪造成功。

## 回归验证

```bash
npm --prefix frontend run test:ui-workbench
npm --prefix frontend run lint
npm --prefix frontend run build
# 在仓库根目录运行完整前端测试：部分历史测试以根目录解析路径。
node --test "frontend/tests/*.test.mjs"
python -m pytest apps/ui_automation/tests apps/executions/tests/test_source_worker_contracts.py apps/executions/tests/test_source_boundary_integrity.py -q
```

工作台专项覆盖编辑保护、完整分页、UUID 恢复、取消与作用域隔离、终态保护、看板刷新、定位器模式和失败修复链接。后端回归覆盖同步兼容、受理幂等、快照、项目授权、broker 失败、取消写入保护、真实 CSS/XPath 语法解析与定位器并发更新。

本次浏览器验证使用真实 Vue/Element Plus/router 组件及完全隔离的 API fixture，未运行真实目标业务操作。目标站点需要登录，未对项目 2 的线上真实数据或部署状态作验收结论。初轮验证记录位于 `.workflow/scratch/20261127-improve-ui-workbench/`，复审修复与回归证据位于 `.workflow/scratch/20261128-fix-ui-workbench-r2/`。
