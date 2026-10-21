# Staging APP 与 TLS/WSS 发布 Gate

这份清单用于获得授权材料后执行 staging 验证。当前只记录 gate，不执行远端写入。

## APP authenticated smoke gate

开始前必须由环境负责人提供并确认：

- approved application session，能够通过 TestHub 应用认证访问 APP API；
- disposable project/test case/device fixture ID；
- fixture 的清理授权和预计保留窗口；
- staging release/build fingerprint 与验证时间窗口；
- APP 设备池中可用设备和设备 capability 约束。

建议顺序：

1. 只读确认 release、health endpoint、用户 session 和项目权限。
2. 获取 device lease，确认 lease token 只返回给当前授权调用者。
3. 发送 heartbeat，验证过期前续租；随后显式 release。
4. 创建一个 disposable unified execution job，验证 dispatch governance、attempt、step、artifact 和 event idempotency。
5. 用已授权 fixture 执行 APP smoke，验证 HTTP API、WebSocket progress、result report 和 lease cleanup。
6. 验证 retry、quality gate、quarantine override audit 和 final result；重放同一 event 不应重复写入。
7. 删除/清理 fixture，确认设备 lease、artifact 和测试数据符合授权的保留策略。

失败时停止该窗口，不修改 source execution 或扩大 fixture 范围。保留 request IDs、job/attempt/event IDs、HTTP/WS 状态和清理结果，禁止记录 token、cookie、密码或原始敏感 payload。

## TLS/WSS change gate

在配置前必须具备并单独审批：

- approved domain/DNS 记录；
- 完整证书链、证书有效期和私钥安全交付；
- 443 监听、firewall/security-group 和反向代理变更授权；
- 独立 TLS/WSS review 与回滚窗口；
- WSS origin、proxy headers、health check 和 websocket upgrade 验证方案。

验证顺序：

1. 在独立变更窗口检查证书链、域名匹配、私钥权限和 TLS 协议配置。
2. 先验证 HTTPS API health/read-only endpoint。
3. 再验证 WSS handshake、认证、progress event、断线重连和超时行为。
4. 检查 HTTP/WS 到 HTTPS/WSS 的跳转和拒绝策略，确认没有开放意外明文入口。
5. 运行独立回滚演练或至少确认可恢复的上一份 proxy/certificate 配置。

当前代码交付保持 staging HTTP/WS 边界。没有 approved domain、证书/私钥、443/firewall 授权或独立审批时，不执行 TLS/WSS 配置。

## Current status

- APP authenticated session：未提供，gate blocked。
- Disposable fixture authorization：未提供，gate blocked。
- TLS/WSS materials and change approval：未提供，gate deferred。
- Local implementation evidence：见 `docs/functional-improvement-acceptance.md`。
