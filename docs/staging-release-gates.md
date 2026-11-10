# Staging APP 与 TLS/WSS 发布 Gate

这份清单用于获得授权材料后执行 staging 验证。当前只记录 gate，不执行远端写入。

## Release candidate validation

### Credential-free browser auth acceptance

Staging health checks prove service availability and static asset delivery only; they do not prove browser authentication behavior. The release validator now also checks a protected API response without credentials and confirms that a private-media probe returns 404. Run credential-free browser auth acceptance separately using a fresh browser profile with no saved cookies, tokens, or local storage. Confirm the login surface loads and a protected route does not expose authenticated content. Record browser results separately from `/health` and `/readiness`; do not enter or capture real credentials for this acceptance.

### Candidate integrity and Nginx validation

For strict candidate validation, verify the v2 manifest's complete inventory, Git commit/tree proof, and `frontend/dist/build-receipt.json`; legacy v1 is rejected by default. Strict frontend receipts require Vite production mode, base `/`, target `es2022`, and the default `VITE_` prefix. The only approved public environment inputs are `VITE_ANALYTICS_ENABLED` and `VITE_QUALITY_INTELLIGENCE_ENABLED`; their effective values are copied into the manifest and must match the receipt. Analytics accepts boolean forms `true`/`false`, `1`/`0`, `yes`/`no`, and `on`/`off`; quality intelligence accepts `true`/`false`. For candidate runtime probes, configure `STAGING_BACKEND_URL` and `STAGING_FRONTEND_URL` to the candidate's actual backend and frontend endpoints; the defaults remain `http://127.0.0.1:18002` and `http://127.0.0.1:18003`. `TESTHUB_ALLOW_LEGACY_RELEASE_MANIFEST=1` is an explicit compatibility opt-in and must be recorded as partial-integrity validation, never as strict source provenance.

A candidate Nginx check must use a temporary copy of that candidate's `staging-nginx/nginx.conf`. The validator selects the binary with `STAGING_NGINX_BIN` (default `/usr/sbin/nginx`) and config with `STAGING_NGINX_CONFIG` (default `$REPO_ROOT/staging-nginx/nginx.conf`). The candidate copy must point `root` at the actual candidate `frontend/dist`, set its actual `listen` address/port and API `proxy_pass` backend, and use candidate-specific PID/log paths. Before HTTP probes, the validator parses the selected Nginx config, requires exactly one server rooted at this candidate's frontend directory, and checks that `STAGING_FRONTEND_URL` matches its listener and `STAGING_BACKEND_URL` matches the direct `/api/` proxy target. A mismatch fails before probing, so the fixed canonical ports cannot silently validate another service. Variable-based or named-upstream `proxy_pass` targets are rejected until they can be resolved unambiguously. `nginx -t -c <candidate-config> -p <candidate-prefix>` parses the supplied main config rather than inheriting host `server` blocks; only includes explicitly named by the candidate config are read. An unmodified config with canonical `/opt/testhub_platform` paths is not proof that another candidate tree was tested.

For the validator's legacy compatibility mode, set `TESTHUB_ALLOW_LEGACY_RELEASE_MANIFEST=1`; the validator passes `--allow-legacy` and reports partial integrity only, without strict source provenance. The default remains strict v2. Do not start or reload Nginx during config-only candidate validation. The canonical staging config must deny `/media/` using `location ^~ /media/`; private media remains accessible only through authenticated APIs.

The frontend build checks also include `npm --prefix frontend run test:auth`. Default pytest collection includes `apps/api_testing/tests.py` and `scripts/tests`; the separate optional UI automation suite installs `requirements-prod.txt` plus `requirements-ui-optional.txt` and runs only mocked runner/browser boundary tests, without launching a real browser.


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
