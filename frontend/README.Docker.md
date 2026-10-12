# TestHub 前端生产部署指南

当前生产方案是在宿主机完成 Vite 构建，再由 Nginx 镜像只读挂载 `dist/` 提供静态文件。

- 前端访问地址：`http://127.0.0.1:8003`
- 后端 Daphne：`http://127.0.0.1:8002`
- 前端 Compose 服务：`testhub_frontend`
- 健康检查：`http://127.0.0.1:8003/health`

## 快速开始

### 使用部署脚本（推荐）

```bash
cd frontend
chmod +x deploy.sh
./deploy.sh start
```

`start` 会在 `frontend/` 中执行 `npm ci` 和 `npm run build`，确认生成 `dist/index.html` 后执行 `docker compose up -d`，最后请求本地健康检查端点。脚本不会读取、打印或传递认证凭据。

常用命令：

```bash
./deploy.sh build    # npm ci + npm run build
./deploy.sh start    # 构建并启动 Nginx
./deploy.sh status   # 查看 Compose 状态并执行一次健康检查
./deploy.sh restart  # 重启现有容器并执行健康检查
./deploy.sh logs
./deploy.sh stop
./deploy.sh clean    # 停止并删除容器、卷；保留 dist 和 Nginx 镜像
```

### 直接使用 Docker Compose

```bash
cd frontend
npm ci
npm run build
docker compose up -d
curl --fail --silent --show-error http://127.0.0.1:8003/health
```

停止服务：

```bash
docker compose down
```

## 生产部署结构

`docker-compose.yml` 使用 `nginx:1.27-alpine`，不在 Docker 中执行 Node 构建：

```yaml
services:
  frontend:
    image: nginx:1.27-alpine
    ports:
      - "8003:80"
    volumes:
      - ./dist:/usr/share/nginx/html:ro
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
```

因此每次发布前都必须在 `frontend/` 执行 `npm ci` 和 `npm run build`。不要使用 `docker compose build`，当前服务没有 `build:` 配置。若 `dist/` 不存在，先运行 `./deploy.sh build`。

Nginx 的 `/api/` 和 `/media/` 请求代理到宿主机的 `8002` 端口；Compose 通过 `host.docker.internal` 访问宿主机后端。后端 Daphne 必须已经监听 `8002`。

## 配置说明

### Nginx

`nginx.conf` 提供以下行为：

- `/` 使用 `try_files` 支持前端 SPA 路由。
- `/api/` 和 `/media/` 代理到 `host.docker.internal:8002`。
- `/health` 返回固定的 `healthy` 文本，不需要认证，也不包含配置或凭据。
- 静态资源启用长期缓存，HTML 使用禁止缓存响应头。

### 前端环境变量

Vite 环境变量会在构建时写入静态资源。需要调整 `VITE_*` 配置时，请在执行构建前设置变量或更新项目环境文件，然后重新执行：

```bash
npm ci
npm run build
```

Compose 中的 `NODE_ENV=production` 只用于容器运行环境，不会替代构建时的 Vite 配置。

## 健康检查和状态

Compose 内置无凭据健康检查：

```yaml
healthcheck:
  test: ["CMD", "wget", "--quiet", "--tries=1", "--spider", "http://127.0.0.1/health"]
```

查看容器状态：

```bash
docker compose ps
docker inspect --format '{{json .State.Health}}' testhub_frontend
```

宿主机健康检查只访问 `127.0.0.1:8003/health`，部署脚本只输出检查是否通过，不输出响应内容、环境变量或服务日志。

## 文件结构

```text
frontend/
├── docker-compose.yml      # Nginx 镜像、8003 端口和 dist 挂载
├── nginx.conf              # SPA、API 代理和健康检查配置
├── deploy.sh               # 构建、启动、状态和清理脚本
├── README.Docker.md        # 本文档
├── package.json
├── package-lock.json
├── vite.config.js
├── dist/                   # npm run build 生成，由 Nginx 只读挂载
└── src/
```

## 故障排查

### 构建失败

```bash
node --version
npm --version
npm ci
npm run build
```

`npm ci` 要求 `package-lock.json` 与 `package.json` 一致。构建成功的标志是 `dist/index.html` 存在。

### 容器无法启动

```bash
docker compose config
docker compose ps
docker compose logs frontend
```

检查 `8003` 是否被占用，并确认 Docker 能访问宿主机的 `8002` 后端。当前部署不使用 80、8001，也不启用 systemd。

### 页面无法访问

```bash
curl --fail --silent --show-error http://127.0.0.1:8003/health
docker exec testhub_frontend nginx -t
docker compose logs frontend
```

如果健康检查通过但页面为空，确认构建产物存在：

```bash
test -f dist/index.html
docker compose config
```

### API 请求失败

确认后端已在 `8002` 监听，并检查 `nginx.conf` 中的代理配置：

```nginx
location /api/ {
    proxy_pass http://host.docker.internal:8002;
}
```

健康检查只验证 Nginx 静态服务，不代表后端认证、数据库或 Redis 可用；这些依赖应通过后端测试和后端监控单独验证。

## 更新部署

```bash
git pull
cd frontend
./deploy.sh start
```

脚本会重新安装锁定版本依赖、重新生成 `dist/`、启动现有 Compose 服务并等待健康检查通过。需要只更新静态资源时执行 `./deploy.sh build`，然后执行 `./deploy.sh restart`。

## 相关命令

```bash
# 查看配置是否可解析
docker compose config

# 查看容器日志
docker compose logs -f frontend

# 进入 Nginx 容器
docker exec -it testhub_frontend sh

# 停止并删除容器
docker compose down
```
