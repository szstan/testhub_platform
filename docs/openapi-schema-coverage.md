# OpenAPI 文档覆盖补齐

本轮在候选工作树为 53 个原先产生 serializer fallback 的 handler/class 补充请求、响应、参数、状态码和文件媒体类型。HTTP 处理函数体、原权限声明和模型均保持原有行为。

## 覆盖范围

- 用户认证、项目成员和版本列表；
- 统一执行导入、metrics、Evidence、CI webhook、QI ingestion 与文档 API；
- API/UI/APP/监控 dashboard、环境和性能比较；
- LLM Judge 与 MCP 配置、目录和审批；
- 需求分析、测试用例下载/同步和测试套件成员操作。

文件上传使用 binary multipart schema，下载声明实际 CSV/TXT/XLSX 媒体类型。固定结果 envelope 逐字段描述；只有实际可变的 JSON 字段保留动态结构。测试套件 remove 接口的 AutoSchema 扩展仅用于描述原先已经使用的 DELETE 请求体。

## Cookie refresh 契约

`CookieTokenRefreshView` 接受可选的 `body.refresh`，或使用 `testhub_refresh` HttpOnly cookie。浏览器 cookie 路径为 `/api/auth/`，浏览器恢复使用 `/api/auth/token/refresh/`。缺失或无效 refresh 返回 401，并清理同路径 cookie。成功 JSON 只有 `access`；refresh 轮换通过 `Set-Cookie` 完成。文档没有可用凭据或 token 示例。

## 基线更新说明

旧文档包含默认推测的空请求、200/204 状态和 SimpleJWT refresh 结构，这些与已存在的实际 handler 行为不一致。本次基线按准确的文档重建：路径由 549 增至 595，operation 由 926 增至 991，原有 operation 没有移除。

基线差异包括原先就必填的输入、实际 201/202/200 状态、整数路径参数、cookie refresh 不暴露 refresh 的响应，以及生成组件/enum 名称变化。它们属于文档补齐；兼容检查器保持严格运行。后续服务端契约变更仍会与更新后的基线比较。客户端生成代码应据新文档刷新。

## 验证

35 项 schema 契约回归覆盖所有原缺失 handler 的真实路由和有结构的响应、请求必填性、敏感输入 write-only、文件类型、cookie 与标准 OpenAPI 有效性。

```bash
RUN_MAIN=false APP_ENV=development DJANGO_SETTINGS_MODULE=backend.test_settings \
  python manage.py spectacular --file /tmp/testhub-schema.yaml --validate
python scripts/api_contract_check.py \
  --baseline docs/openapi-contract-baseline.yaml \
  --current /tmp/testhub-schema.yaml
```

本轮生成错误为 0；尚有 131 项独立警告，主要涉及既有字段提示和 enum 命名。不存在以 `exclude`、空 serializer 或关闭诊断来伪造覆盖的处理。真实 Linux/MySQL、浏览器认证和来源 canary 仍需各自的候选环境验收证据。
