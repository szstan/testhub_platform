# TestHub 正式交付清单

本清单用于把已经通过 CI 和 staging 的 release 交付到正式环境。发布必须使用仓库生成的 archive、release manifest 和已校验的 Linux wheelhouse；生产运行目录不直接从工作树复制。

## 发布包

交付包应同时保留以下证据：

- Git commit SHA 和 release identifier；
- `testhub-platform-<release>.tar.gz` 及 SHA256；
- archive 内的 `release-manifest.json`；
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
2. 在候选目录解压 archive，继承上一版本的 `.env`、`logs`、`media`、`static_files`、`staging-nginx` 和两个 venv。
3. 校验 release manifest、requirements hash、runtime dependency versions 和 wheelhouse manifest。
4. 以 `testhub` 身份运行 `manage.py check`、`migrate --plan`；确认 migration plan 后应用 migration。
5. 停止 backend、Celery 和 scheduler，保留上一版本 rollback 目录，然后原子切换候选目录。
6. 启动服务并 reload Nginx。
7. 运行 `scripts/validate_staging_release.sh`，确认 readiness 的 database、cache、worker、channel、migrations、frontend_assets 全部 `ok`。
8. 运行 strict pilot，要求 `max_unmatched=0`。
9. 保留 release、manifest、日志、readiness、pilot 和 rollback 证据。

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
