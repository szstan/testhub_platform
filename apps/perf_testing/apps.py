from django.apps import AppConfig
import logging


logger = logging.getLogger(__name__)


class PerfTestingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.perf_testing'
    verbose_name = '性能测试'

    def ready(self):
        """启动自愈：回收 Django 重启前遗留的僵尸执行。

        仅在主进程执行一次（避免 runserver 自动重载时重复执行），
        且失败不得影响服务启动。
        """
        import apps.perf_testing.checks  # noqa: F401
        import os
        import sys

        # runserver 自动重载会启动两个进程，只在真正的工作进程执行
        if os.environ.get('RUN_MAIN') == 'false':
            return
        # 迁移/收集静态等管理命令不做自愈
        argv = ' '.join(sys.argv)
        for skip in ('makemigrations', 'migrate', 'collectstatic', 'run_perf_execution', 'shell'):
            if skip in argv:
                return

        try:
            from apps.perf_testing.services.cleanup import reap_stale_executions
            reap_stale_executions(startup=True)
        except Exception:  # noqa: BLE001 - startup recovery must not block service boot
            logger.exception('Failed to reap stale performance executions during startup')
