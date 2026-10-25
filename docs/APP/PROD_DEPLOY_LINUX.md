# 生产部署（Linux）

适用路径：`/opt/testhub_platform`
后端域名/IP：`172.13.6.230`
前端部署：独立部署在 `/opt/testhub_platform/frontend/dist`

> 当前共享主机的 TestHub 发布边界：Daphne 使用 `8002`，前端 Nginx 容器使用 `8003`；既有 `80/8001` 服务不属于 TestHub 发布流程，不得停止或重配置。前端构建、Compose、健康检查和代理配置以 [frontend/README.Docker.md](../../frontend/README.Docker.md) 为准。当前 systemd 服务保持 `active` 但 `disabled`，不要在发布脚本中自动 `enable`。

---

## 1) 创建用户与目录

```bash
sudo useradd -m -s /bin/bash testhub
sudo mkdir -p /opt/testhub_platform
sudo chown -R testhub:testhub /opt/testhub_platform
```

将代码放到：
```
/opt/testhub_platform
```

目录结构示例：
```
/opt/testhub_platform/
├── backend/
├── apps/
├── manage.py
├── requirements.txt
├── media/
├── static/
└── frontend/
```

---

## 2) Python 依赖与虚拟环境

Web/Daphne 环境只安装仓库中的核心生产依赖清单 `requirements-prod.txt`。不要在生产 Web venv 执行 `pip install -r requirements.txt`，该文件包含面向开发或自动化场景的额外依赖。

```bash
cd /opt/testhub_platform
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
command -v "$PYTHON_BIN"
"$PYTHON_BIN" -m venv venv-prod
source venv-prod/bin/activate
MIRROR="${TESTHUB_MIRROR:-/var/lib/testhub-mirror/testhub_platform/current}"
test -d "$MIRROR"
test -f "$MIRROR/manifest.json"
python -m pip install --no-index --no-cache-dir --find-links "$MIRROR" -r requirements-prod.txt
python scripts/validate_linux_runtime.py --profile web --repo-root "$PWD" --mirror "$MIRROR"
```

### 可选自动化依赖边界

以下清单只用于独立自动化执行环境，当前 Web/Daphne 服务不安装、不启动这些能力：

- `requirements-ui-optional.txt`：Playwright、Selenium、webdriver-manager、browser-use 及其固定版本的 cdp-use、bubus 运行时依赖。
- `requirements-app-optional.txt`：Airtest、EasyOCR、OpenCV、NumPy（loguru、Pillow 已由 `requirements-prod.txt` 提供）。
- `requirements-report-optional.txt`：Allure pytest 插件及 Python 运行时；仓库内 `allure/bin/allure` 提供报告命令。

APP worker 必须使用独立的 `venv-automation`，不要复用 `venv-prod`。安装后先执行与运行时相同的依赖和可执行文件检查：

```bash
cd /opt/testhub_platform
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
command -v "$PYTHON_BIN"
"$PYTHON_BIN" -m venv venv-automation
source venv-automation/bin/activate
MIRROR="${TESTHUB_MIRROR:-/var/lib/testhub-mirror/testhub_platform/current}"
test -d "$MIRROR"
python -m pip install --no-index --no-cache-dir --find-links "$MIRROR" -r requirements-prod.txt
python -m pip install --no-index --no-cache-dir --find-links "$MIRROR" -r requirements-app-optional.txt
python -m pip install --no-index --no-cache-dir --find-links "$MIRROR" -r requirements-report-optional.txt
python scripts/validate_linux_runtime.py --profile automation --repo-root "$PWD" --mirror "$MIRROR"
export APP_ADB_PATH="$(command -v adb)"
test -n "$APP_ADB_PATH"
sudo -u testhub -- "$APP_ADB_PATH" version
```

`APP_ADB_PATH` 必须指向运行 worker 的服务用户可执行的 ADB 文件；不要只在交互式管理员 shell 中验证。缺少 Airtest、Allure Python 插件/commons、仓库内 Allure 命令或 ADB 时，`AppTestExecutor` 会在启动 pytest 前失败并记录明确原因。

### 可重复依赖制品（Linux/Python 3.12）

`scripts/build_linux_wheelhouse.py` 只能在原生 Linux/Python 3.12 x86_64 构建主机运行。输出目录必须位于仓库外；脚本会解析三份 requirements 的完整传递依赖，补充源码包构建依赖，拒绝 Windows/macOS/musllinux 文件，并生成 `manifest.json`、`manifest.sha256` 和 `SHA256SUMS`。脚本只构建本地制品，不负责上传；上传前必须使用已批准的内部 artifact 仓库或 package mirror。

```bash
cd /opt/testhub_platform
RELEASE="$(sed -n '1p' RELEASE 2>/dev/null || date +%Y%m%d%H%M%S)"
WHEELHOUSE="/var/tmp/testhub-wheelhouse-${RELEASE}"
python3.12 scripts/build_linux_wheelhouse.py \
  --output "$WHEELHOUSE" \
  --index-url "${APP_INDEX_URL:-https://pypi.org/simple}"
```

候选环境必须使用 manifest 校验过的 wheelhouse，并保持与正式 venv 隔离：

```bash
MIRROR="${TESTHUB_MIRROR:-/var/lib/testhub-mirror/testhub_platform/current}"
test -d "$MIRROR"
test -f "$MIRROR/manifest.json"
CANDIDATE="/var/tmp/testhub-venv-${RELEASE}"
python3.12 -m venv "$CANDIDATE"
"$CANDIDATE/bin/python" -m pip install --no-index --no-cache-dir \
  --find-links "$MIRROR" \
  -r requirements-prod.txt \
  -r requirements-app-optional.txt \
  -r requirements-report-optional.txt
"$CANDIDATE/bin/python" scripts/validate_linux_runtime.py \
  --profile automation \
  --repo-root "$PWD" \
  --mirror "$MIRROR"
```

不要把 wheelhouse、manifest 中的本地路径或任何内部访问凭据提交到 Git；内部制品的不可变版本、SHA-256、所有者和保留策略由发布平台管理。

API 测试管理页面和核心 API 服务随 Web 环境发布；需要浏览器、设备、OCR 或 Allure 的执行 worker 属于独立部署边界。未安装可选依赖时，Web 登录、项目/用例/套件管理、执行记录、报告统计和评审 API 仍应可用。


---

## 3) 环境变量（.env）

创建 `/opt/testhub_platform/.env`：

```env
APP_ENV=production
DEBUG=False
SECRET_KEY=replace-with-a-random-secret
ALLOWED_HOSTS=172.13.6.230

DB_NAME=testhub
DB_USER=root
DB_PASSWORD=your_db_password
DB_HOST=127.0.0.1
DB_PORT=3306
APP_ADB_PATH=/usr/bin/adb

REDIS_URL=redis://:1234@127.0.0.1:6379/0
```

---

## 4) 收集静态文件

```bash
source /opt/testhub_platform/venv-prod/bin/activate
cd /opt/testhub_platform
python manage.py collectstatic --noinput
```

---

## 5) 发布制品与 staging 发布后检查

源码发布包必须由仓库脚本生成，脚本会拒绝缺少前端生产构建产物的工作树：

```bash
cd /opt/testhub_platform
python scripts/build_release_archive.py \
  --output "/var/tmp/testhub-platform-${RELEASE}.tar.gz" \
  --release "${RELEASE}"
sha256sum "/var/tmp/testhub-platform-${RELEASE}.tar.gz"
```

发布候选切换完成后，在目标机执行：

```bash
cd /opt/testhub_platform
bash scripts/validate_staging_release.sh
```

该检查会验证 `frontend/dist/index.html`、Django system check、迁移计划、SELinux 上下文、Nginx 用户可读性、后端 health/readiness、前端 health/index 和 API docs。SELinux enforcing 时如果没有 `restorecon`，检查会失败。发布同步应排除 `.env`、`media/`、`static_files/`、虚拟环境、依赖 wheelhouse、日志、备份和 `frontend/node_modules/`，并保留独立 rollback 目录。

---

## 6) systemd 服务

### 6.1 ASGI 服务（Daphne）
`/etc/systemd/system/testhub-asgi.service`

```ini
[Unit]
Description=TestHub ASGI (Daphne)
After=network.target

[Service]
User=testhub
WorkingDirectory=/opt/testhub_platform
Environment="DJANGO_SETTINGS_MODULE=backend.settings"
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=/opt/testhub_platform/.env
ExecStart=/opt/testhub_platform/venv-prod/bin/daphne -b 0.0.0.0 -p 8000 backend.asgi:application
Restart=always

[Install]
WantedBy=multi-user.target
```

### 6.2 Celery Worker 服务
`/etc/systemd/system/testhub-celery.service`

```ini
[Unit]
Description=TestHub Celery Worker
After=network.target

[Service]
User=testhub
WorkingDirectory=/opt/testhub_platform
Environment="DJANGO_SETTINGS_MODULE=backend.settings"
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=/opt/testhub_platform/.env
ExecStart=/opt/testhub_platform/venv-automation/bin/celery -A backend worker --loglevel=info --pool=solo --concurrency=1
Restart=always

[Install]
WantedBy=multi-user.target
```

### 6.3 启动服务
```bash
sudo systemctl daemon-reload
sudo systemctl enable testhub-asgi testhub-celery
sudo systemctl start testhub-asgi testhub-celery
```

### 6.4 状态与日志
```bash
sudo systemctl status testhub-asgi
sudo systemctl status testhub-celery

journalctl -u testhub-asgi -f
journalctl -u testhub-celery -f
```

---

## 7) Nginx 配置（含 WebSocket）

`/etc/nginx/conf.d/testhub.conf`

```nginx
server {
    listen 80;
    server_name 172.13.6.230;

    # 静态与媒体
    location /static/ {
        alias /opt/testhub_platform/static/;
    }

    # Never expose MEDIA_ROOT directly. Reports, recordings, and downloads
    # must use their authenticated API endpoints.
    location ^~ /media/ {
        return 404;
    }

    # API
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # WebSocket
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }

    # 前端独立部署
    location / {
        root /opt/testhub_platform/frontend/dist;
        try_files $uri /index.html;
    }
}
```

重启 Nginx：
```bash
sudo nginx -t
sudo systemctl restart nginx
```

---

## 8) 前端构建（独立部署）

```bash
cd /opt/testhub_platform/frontend
npm install
npm run build
```

---

## 9) 验证

推荐直接执行仓库内检查脚本：

```bash
cd /opt/testhub_platform
bash scripts/validate_staging_release.sh
```

APP authenticated smoke 和 TLS/WSS 必须分别按照 `docs/staging-release-gates.md` 的授权边界执行。

---

## 10) 一键部署脚本（可选）

> 脚本会写入 `.env`、systemd 与 Nginx 配置，请先确认变量值（如 DB/Redis 密码）。

保存为 `/opt/testhub_platform/deploy_prod.sh`：

```bash
#!/usr/bin/env bash
set -e

APP_DIR="/opt/testhub_platform"
VENV_DIR="$APP_DIR/venv-prod"
AUTOMATION_VENV_DIR="$APP_DIR/venv-automation"
NGINX_CONF="/etc/nginx/conf.d/testhub.conf"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
MIRROR="${TESTHUB_MIRROR:-/var/lib/testhub-mirror/testhub_platform/current}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.12 interpreter is required: $PYTHON_BIN" >&2
  exit 1
fi
"$PYTHON_BIN" --version

test -d "$MIRROR"
test -f "$MIRROR/manifest.json"

APP_ADB_PATH="${APP_ADB_PATH:-$(command -v adb || true)}"
test -n "$APP_ADB_PATH"
test -x "$APP_ADB_PATH"

echo "=== 1) 创建虚拟环境 ==="
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
if [ ! -d "$AUTOMATION_VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$AUTOMATION_VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

echo "=== 2) 安装 Web 依赖 ==="
pip install --no-index --no-cache-dir --find-links "$MIRROR" -r "$APP_DIR/requirements-prod.txt"
"$VENV_DIR/bin/python" "$APP_DIR/scripts/validate_linux_runtime.py" \
  --profile web --repo-root "$APP_DIR" --mirror "$MIRROR"

echo "=== 3) 安装 APP worker 依赖并执行预检 ==="
"$AUTOMATION_VENV_DIR/bin/pip" install --no-index --no-cache-dir --find-links "$MIRROR" -r "$APP_DIR/requirements-prod.txt"
"$AUTOMATION_VENV_DIR/bin/pip" install --no-index --no-cache-dir --find-links "$MIRROR" -r "$APP_DIR/requirements-app-optional.txt"
"$AUTOMATION_VENV_DIR/bin/pip" install --no-index --no-cache-dir --find-links "$MIRROR" -r "$APP_DIR/requirements-report-optional.txt"
"$AUTOMATION_VENV_DIR/bin/python" "$APP_DIR/scripts/validate_linux_runtime.py" \
  --profile automation --repo-root "$APP_DIR" --mirror "$MIRROR"
"$APP_ADB_PATH" version
sudo -u testhub -- "$APP_ADB_PATH" version
test -x "$APP_DIR/allure/bin/allure"
"$APP_DIR/allure/bin/allure" --version

echo "=== 4) 写入 .env ==="
: "${SECRET_KEY:?请通过安全环境注入 SECRET_KEY}"
: "${DB_PASSWORD:?请通过安全环境注入 DB_PASSWORD}"
: "${REDIS_URL:?请通过安全环境注入 REDIS_URL}"
cat > "$APP_DIR/.env" <<EOF
APP_ENV=production
DEBUG=False
SECRET_KEY=$SECRET_KEY
ALLOWED_HOSTS=${ALLOWED_HOSTS:?请设置 ALLOWED_HOSTS}

DB_NAME=${DB_NAME:-testhub}
DB_USER=${DB_USER:-root}
DB_PASSWORD=$DB_PASSWORD
DB_HOST=${DB_HOST:-127.0.0.1}
DB_PORT=${DB_PORT:-3306}

APP_ADB_PATH=$APP_ADB_PATH
REDIS_URL=$REDIS_URL
EOF

echo "=== 5) 收集静态文件 ==="
python "$APP_DIR/manage.py" collectstatic --noinput

echo "=== 6) systemd 服务 ==="
cat > /etc/systemd/system/testhub-asgi.service <<EOF
[Unit]
Description=TestHub ASGI (Daphne)
After=network.target

[Service]
User=testhub
WorkingDirectory=$APP_DIR
Environment="DJANGO_SETTINGS_MODULE=backend.settings"
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=$APP_DIR/.env
ExecStart=$VENV_DIR/bin/daphne -b 0.0.0.0 -p 8000 backend.asgi:application
Restart=always

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/testhub-celery.service <<EOF
[Unit]
Description=TestHub Celery Worker
After=network.target

[Service]
User=testhub
WorkingDirectory=$APP_DIR
Environment="DJANGO_SETTINGS_MODULE=backend.settings"
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=$APP_DIR/.env
ExecStart=$AUTOMATION_VENV_DIR/bin/celery -A backend worker --loglevel=info --pool=solo --concurrency=1
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable testhub-asgi testhub-celery
systemctl restart testhub-asgi testhub-celery

echo "=== 7) Nginx 配置 ==="
cat > "$NGINX_CONF" <<EOF
server {
    listen 80;
    server_name 172.13.6.230;

    location /static/ {
        alias /opt/testhub_platform/static/;
    }

    # Do not publish MEDIA_ROOT through Nginx; private artifacts are served
    # by authenticated API actions only.
    location ^~ /media/ {
        return 404;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
    }

    location / {
        root /opt/testhub_platform/frontend/dist;
        try_files \$uri /index.html;
    }
}
EOF

nginx -t
systemctl restart nginx

echo "=== ✅ 部署完成 ==="
```

执行：
```bash
sudo chmod +x /opt/testhub_platform/deploy_prod.sh
sudo /opt/testhub_platform/deploy_prod.sh
```

