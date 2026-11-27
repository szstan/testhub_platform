# Android Appium 自动化主机部署

## 运行环境

Appium 和 UiAutomator2 运行在连接 Android 设备的 Linux 自动化主机。创建执行会话的 Celery worker 与交互采集 worker 需要访问同一台主机的 Appium 实例及 ADB 设备。Web 使用 `requirements-prod.txt`，两个 APP worker 使用 `requirements-prod.txt`、`requirements-app-optional.txt` 和 `requirements-report-optional.txt`。APP 虚拟环境单独安装 Selenium，避免与 Web UI 兼容引擎的版本冲突。

| 组件 | 发布候选版本 |
| --- | --- |
| Node.js / npm | 22.23.1 / 10.9.8 |
| Appium Server / UiAutomator2 | 3.2.0 / 5.0.1 |
| JDK / Python | JDK 17 / Python 3.12 |
| Appium-Python-Client / Selenium | 6.0.0 / 4.32.0 |

在目标 Linux 主机记录 JDK 构建版本和 `adb version`，核对测试设备的 Android 版本、API 级别和分辨率。上表是制品构建候选组合，目标组合由真机验收确认。构建、校验和安装 Python wheelhouse 见 [Linux 部署说明](PROD_DEPLOY_LINUX.md)。

在自动化制品目录准备 Appium Server 和驱动。保存 npm 锁文件及安装好的驱动制品，将它们与 Python wheelhouse 一起用于预发。用执行服务账号运行驱动预检。

```bash
npm install --prefix /opt/testhub-appium --save-exact appium@3.2.0
APPIUM_HOME=/var/lib/testhub-appium /opt/testhub-appium/node_modules/.bin/appium driver install uiautomator2@5.0.1
sudo -u testhub -- env APPIUM_HOME=/var/lib/testhub-appium /opt/testhub-appium/node_modules/.bin/appium driver doctor uiautomator2
```

## 实例与队列

为验收设备分配固定的本机 HTTP 端口。将设备数据库 ID 或 ADB 序列号与实例地址写入 `APP_APPIUM_ENDPOINTS_JSON`，格式为 `{"test-device-serial":"http://127.0.0.1:4723"}`。Web 服务会在创建会话并入队前校验该映射，APP 执行与交互 worker 会使用同一映射确认会话端点；因此须在 Web、`app_execution` 和 `app_interactive` 服务环境中设置相同的值，或让各进程从项目根目录的 `.env` 读取同一配置。未配置时普通 Django 服务仍可启动，但首次使用 Appium 功能会返回配置错误。映射中每个地址必须是带端口的 HTTP literal loopback 地址（`127.0.0.1` 或 `::1`），不支持主机名、HTTPS 或非回环地址。设备的 `systemPort` 按默认基数 8200 加设备记录 ID 分配，核对这些端口在主机上可用。应用包名来自有权限的项目用例或采集请求。如下命令可作为每台设备的 systemd `ExecStart`，按设备替换 `4723`。服务使用 `testhub` 账号、固定的 `APPIUM_HOME` 和 Android SDK/JDK 环境。将 systemd `StandardOutput` 和 `StandardError` 设为 `null`，Appium 服务状态通过 systemd 与只读预检观察，避免请求参数和输入值写入服务日志。

```bash
/opt/testhub-appium/node_modules/.bin/appium --address 127.0.0.1 --port 4723 --log-level error
```

将原有普通 Celery worker 绑定到 `celery`。三个进程各用不同的 worker 名称，初期 APP 执行与交互进程分别使用 `--pool=solo --concurrency=1`，可在两台设备同时执行和采集。共享设置 `APP_EXECUTION_QUEUE=app_execution` 之后，单用例、套件、批次和统一执行外层 `execute_app_source` 路由到 APP 执行 worker。需要从统一执行发起 APP 测试的部署，把 `app=apps.executions.source_workers.execute_app_source` 合并到已有的 `EXECUTION_DISPATCH_ROUTES` 中。核对定时与数据集任务最终提交到 APP 队列。

```bash
/opt/testhub_platform/venv-automation/bin/celery -A backend worker -Q app_execution -n app-execution@%h --pool=solo --concurrency=1
/opt/testhub_platform/venv-automation/bin/celery -A backend worker -Q app_interactive -n app-interactive@%h --pool=solo --concurrency=1
```

Web 和采集进程使用同一个共享 Django 缓存，生产环境可沿用默认 Redis 配置。控件树、命中详情与截图结果在缓存中保留 120 秒。会话和操作状态由数据库保存，可通过执行与设备管理界面排查。

上线前运行预检，`--appium-url` 可为每个设备实例重复指定。

```bash
/opt/testhub_platform/venv-automation/bin/python scripts/validate_linux_runtime.py \
  --profile automation --appium-url http://127.0.0.1:4723 --appium-version 3.2.0
```

预检确认服务就绪及报告版本。使用测试 APK 建立会话、阅读控件树和关闭会话属于真机验收。设备进入待恢复时，先取得该设备的执行或采集记录，确认 worker、Appium 实例和设备端操作已结束。必要时重启该设备对应的实例。预检通过后使用设备管理页的恢复操作重新开放设备。

## 项目启用与回退

1. 创建一个测试项目并绑定测试应用和专用设备。用普通项目成员完成点选控件、唯一性验证、创建关联、执行和回看失败步骤。
2. 在两台设备分别运行登录和重复 ID 列表用例。每台设备每条用例运行 20 次，每次恢复初始状态。每条至少通过 19/20 次，80 次总计零误点，页面已就绪时定位 P95 不超过 2 秒。验证零命中、多命中时均无点击。正常停止后在 15 秒内重获租约。演练断开会话、待恢复、混合套件及定时执行。
3. 记录设备、APK 版本、运行 ID、结果、定位耗时、误点数量与回收时间。验收通过后启用项目的 Appium 默认引擎。复制原用例，重新采集定位并试跑。验证后更新套件、计划任务和统一执行的用例引用。
4. 回退时暂停接收新的 Appium 执行，收尾或取消已提交记录，确认会话终止。把受影响套件与计划任务切回保留的旧用例。保留 Appium 用例与历史结果以供修复后恢复。

| 设备 / Android 版本 | APK 版本 | 用例 | 执行 ID | 通过 / 总次数 | 误点 | 定位 P95 | 正常取消回收耗时 |
| --- | --- | --- | --- | --- | --- | --- | --- |
|  |  | 登录并断言首页 |  |  / 20 |  |  |  |
|  |  | 重复 ID 列表点击指定行 |  |  / 20 |  |  |  |
|  |  | 登录并断言首页 |  |  / 20 |  |  |  |
|  |  | 重复 ID 列表点击指定行 |  |  / 20 |  |  |  |
