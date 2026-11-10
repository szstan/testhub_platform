# TestHub 正式交付清单

本清单用于把已经通过 CI 和 staging 的 release 交付到正式环境。发布必须使用仓库生成的 archive、release manifest 和已校验的 Linux wheelhouse；生产运行目录不直接从工作树复制。

## Strict release archive (manifest v2)

正式交付默认要求 manifest v2。它记录完整 archive 文件清单、对应 Git commit/tree 证明，以及 `frontend/dist/build-receipt.json`；校验会确认 receipt 与 source commit/tree 和实际前端产物一致，并要求 Vite production mode、固定的 `/` base、`es2022` target、默认 `VITE_` prefix，以及仅使用 `VITE_ANALYTICS_ENABLED` / `VITE_QUALITY_INTELLIGENCE_ENABLED` 两个批准的前端环境输入。有效环境值会同时写入 receipt 和 manifest 并交叉校验。正式 archive 必须从 clean checkout 构建。Vite 可为本地 dirty 开发构建生成 receipt，但 archive builder 会拒绝 dirty source、非 production mode、不批准的构建输入、过期 receipt 或与 archive/source 不匹配的 receipt。

```bash
test -z "$(git status --porcelain)"
npm --prefix frontend ci
npm --prefix frontend run build
python scripts/build_release_archive.py \
  --output "testhub-platform-${RELEASE_ID}.tar.gz" \
  --release "${RELEASE_ID}"
python scripts/validate_release_manifest.py \
  --archive "testhub-platform-${RELEASE_ID}.tar.gz" \
  --release "${RELEASE_ID}"
```

Legacy v1 校验仅为明确的过渡兼容路径：使用 `--allow-legacy` 时只获得部分完整性检查，不具备完整 inventory、Git commit/tree 或 frontend build receipt 的严格 source proof；不得据此声明严格来源验证或 v2 release 已通过。


## 发布包

交付包应同时保留以下证据：

- Git commit SHA、Git tree 和 release identifier；
- `testhub-platform-<release>.tar.gz` 及 SHA256；
- archive 内的 v2 `release-manifest.json`、完整 inventory 和 `frontend/dist/build-receipt.json`；
- wheelhouse `manifest.json`、`manifest.sha256` 和 `SHA256SUMS`；
- requirements-prod、APP、report、UI optional 四份文件的 hash；
- migration plan/apply 输出；
- CI run、staging validator、readiness 和 strict pilot 链接；
- rollback 目录路径和生成时间。

在 Linux/Python 3.12 x86_64 构建依赖制品时使用已批准的镜像，例如清华镜像或内部 mirror：

```bash
python scripts/build_linux_wheelhouse.py \
  --output /var/tmp/testhub-wheelhouse-<release> \
  --requirements requirements-prod.txt \
  --requirements requirements-app-optional.txt \
  --requirements requirements-report-optional.txt \
  --requirements requirements-ui-optional.txt \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple

python scripts/validate_wheelhouse_manifest.py \
  --directory /var/tmp/testhub-wheelhouse-<release> \
  --requirements requirements-prod.txt \
  --requirements requirements-app-optional.txt \
  --requirements requirements-report-optional.txt \
  --requirements requirements-ui-optional.txt
```

生产或候选 venv 必须从已验证 mirror 使用 `--no-index --find-links` 安装。Web venv 只安装 `requirements-prod.txt`；automation venv 还安装 APP、report 和 UI optional requirements。安装后运行 `pip check` 和 `scripts/validate_runtime_dependencies.py`。

## 切换顺序

1. 冻结 release commit，构建 archive 和 manifest。
2. 在候选目录解压 archive，继承上一版本 `.env`、`logs`、`media`、`static_files` 和两个 venv；保留其原有服务账号 ownership，不对 release/runtime 目录做递归 `chown`，也不改动 MySQL 数据目录。`staging-nginx/nginx.conf` 使用 release archive 内的版本，不从旧 rollback 继承。
3. 校验 release manifest、requirements hash、runtime dependency versions 和 wheelhouse manifest。
4. 以 `testhub` 身份运行 `manage.py check`、`migrate --plan`；确认 migration plan 后应用 migration。
5. 停止 backend、Celery 和 scheduler，保留上一版本 rollback 目录，然后原子切换候选目录。
6. 启动服务并 reload Nginx。
7. 运行 `scripts/validate_staging_release.sh`，确认 readiness 的 database、cache、worker、channel、migrations、frontend_assets 全部 `ok`。
8. 运行 strict pilot，要求 `max_unmatched=0`。
9. 保留 release、manifest、日志、readiness、pilot 和 rollback 证据。

## Candidate Nginx validation

The staging validator selects its binary with `STAGING_NGINX_BIN` (default `/usr/sbin/nginx`) and its config with `STAGING_NGINX_CONFIG` (default `$REPO_ROOT/staging-nginx/nginx.conf`). It runs `nginx -t -c "$STAGING_NGINX_CONFIG" -p "$(dirname "$STAGING_NGINX_CONFIG")/"`; `-c` parses the supplied main config and does not merge the host `/etc/nginx/nginx.conf` or its `conf.d` server blocks, except for files explicitly included by that candidate config.

For a candidate tree, point `STAGING_NGINX_CONFIG` at a temporary copy of the candidate config. That copy must use the candidate's actual `frontend/dist` asset root, candidate listen address/port, API `proxy_pass` backend, and candidate-specific PID/log paths. The validator parses the candidate config and requires one server rooted at the candidate's `frontend/dist`, one probe listener, and a direct `/api/` proxy target. `STAGING_FRONTEND_URL` must match the configured listener and `STAGING_BACKEND_URL` must match that proxy target; mismatches fail before HTTP requests. Named or variable-based upstreams fail closed. Defaults are `http://127.0.0.1:18003` and `http://127.0.0.1:18002`, so candidate deployments on other ports must set both URLs to the candidate endpoints.

Example for an already deployed candidate at `/srv/testhub/candidate`:

```bash
STAGING_NGINX_BIN=/usr/sbin/nginx STAGING_NGINX_CONFIG=/var/tmp/testhub-candidate-nginx.conf STAGING_FRONTEND_URL=http://127.0.0.1:28003 STAGING_BACKEND_URL=http://127.0.0.1:28002 bash scripts/validate_staging_release.sh /srv/testhub/candidate
```

The validator also checks that an unauthenticated request through the candidate frontend to `/api/executions/profiles/` returns 401 or 403, and that `/media/__testhub_private_probe__.png` returns 404. These probes are credential-free; no real staging deployment or secret is needed for the local implementation tests.

`TESTHUB_ALLOW_LEGACY_RELEASE_MANIFEST=1` explicitly enables legacy v1 compatibility in this validator. Its result is partial integrity only, not strict source provenance; leave it unset for normal v2 validation. Do not start or reload Nginx as part of config-only candidate validation.

The canonical staging config denies `/media/` with a `^~` location. Private media stays behind authenticated API actions; never add a public `MEDIA_ROOT` alias.

## Frontend deployment boundary

`frontend/deploy.sh build` stages `dist` below `frontend/` so the final rename stays on one filesystem. Before promotion it sets artifact directories to `0755` and files to `0644`; after promotion it applies `restorecon` at the canonical `dist` path and verifies Nginx traversal/read access. Artifact discovery failures and per-path access failures propagate through checked `find -exec` status. Build, validation, or rename failures preserve/restore the prior `dist`. This frontend-only operation does not change ownership or contents of `.env`, backend runtime directories, or MySQL data.

## Unified execution route gate

默认保持：

```env
EXECUTION_DISPATCH_ROUTES=
EXECUTION_RETRY_DISPATCH_ROUTES=
```

只有 source binding、真实 worker、项目权限和 disposable canary fixture 都已准备好时，才逐个 source 启用 route。每次只启用一个 source，并验证 dispatch、attempt、result event、retry、cancel、final state 和 cleanup。canary 完成后删除 fixture，并恢复 route 配置。

APP route 还需要 device capability、lease heartbeat/release、approved APK/session 和清理授权。Performance route 还需要可回写 `run_finished` 的 completion callback。没有这些条件时保持 fail-closed。

## TLS/WSS gate

TLS/WSS 是独立网络变更窗口，不能用 HTTP staging 或自签证书代替。开始前必须具备：

- approved domain/DNS；
- 证书链和私钥；
- 443 firewall/proxy/Nginx 变更；
- 独立 review 和 rollback window。

验证顺序为 HTTPS health、HTTPS readiness、HTTPS API docs、WSS authentication、WSS progress、result report 和 rollback。缺少任一材料时保持 HTTP/WS，不宣称 TLS/WSS 已交付。
