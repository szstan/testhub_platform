# TestHub 50 用户运行基线

本基线面向 50 个注册用户、10–20 个同时在线用户、5–10 个并行执行任务和 10–30 台 APP/UI 自动化设备。目标是保持模块化单体，降低部署和运维成本。

## 推荐部署

- Web/API：Django + Daphne + Channels
- 异步任务：Celery + Redis
- 数据库：MySQL 8，`utf8mb4`
- 入口：Nginx 或 Caddy，统一处理 HTTPS/WSS
- APP/UI worker：独立主机或独立进程，靠近 ADB/浏览器设备
- 报告、截图和大型附件：对象存储或独立磁盘

Web、MySQL 和 Redis 可以先放在一台 8 vCPU/16 GB 服务器上。自动化 worker 建议使用独立的 8 vCPU/16 GB 主机，避免设备、OCR、浏览器和截图任务影响 Web 请求。

## 容量边界

- 同时在线用户：20
- 并行执行任务：5–10
- APP/UI 设备：10–30
- 普通 API p95：2 秒以内
- WebSocket 状态更新：通常 3 秒以内
- 租约过期释放：1 分钟以内
- 发布失败恢复上一版本：15 分钟以内

超过这些边界时，先增加 worker 和设备节点，再评估是否需要拆分服务。暂不需要 Kubernetes、Kafka 或 Temporal。

## 生产配置

生产环境应使用 Redis 作为跨进程缓存和 Channels/Celery 的共享依赖，并根据连接数调整：

```env
DB_CONN_MAX_AGE=60
CELERY_TASK_TRACK_STARTED=True
CELERY_WORKER_PREFETCH_MULTIPLIER=1
CACHE_URL=redis://:password@127.0.0.1:6379/0
```

测试环境可以继续覆盖为 LocMemCache 和 InMemoryChannelLayer。生产环境不要依赖 LocMemCache，因为多个 Daphne/Celery 进程之间不会共享缓存。

## 发布检查

每次发布至少保留以下步骤的输出：

1. 生成带 commit SHA 和 SHA-256 的不可变制品。
2. 在候选目录运行 `python manage.py check`。
3. 在目标 MySQL 上运行 `python manage.py migrate --plan` 和 `showmigrations`。
4. 执行迁移后再启动应用 worker。
5. 检查 `/health/` 和 `/readiness/`。
6. 检查 `/api/docs/`、Celery、WebSocket 和设备 worker 状态。
7. 保留上一版本和 shared runtime，失败时恢复上一版本。

归档、`.env`、media、logs、虚拟环境和静态运行目录要分开管理。制品上传使用 artifact registry、对象存储或 SFTP，避免使用临时 HTTP 服务作为正式发布通道。

## 备份和恢复

- MySQL 每日备份，保留 7–14 天。
- 每月至少执行一次真实恢复演练。
- 发布前确认当前数据库备份时间和可恢复版本。
- 迁移失败时停止发布，保留 migration log 和残留 DDL 信息，不重复执行未知状态的迁移。

## APP 自动化

设备池继续使用数据库租约即可，但每台设备必须有 capability、健康状态、最后心跳、最大租用时长和隔离状态。执行结束必须释放 lease、清理临时应用和测试数据。未经批准的 session、fixture 或设备 lease 不得用于 staging smoke。

## 暂缓能力

在真实容量超过上述边界前，以下能力保持评估状态：Kubernetes、Temporal、Kafka、完整 STF 设备农场、多地域部署和微服务拆分。
