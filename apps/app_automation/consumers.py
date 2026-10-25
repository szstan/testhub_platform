import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

logger = logging.getLogger(__name__)


class AppExecutionConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        try:
            self.execution_id = self.scope["url_route"]["kwargs"]["execution_id"]
            user = self.scope.get('user')
            if user is None or not getattr(user, 'is_authenticated', False):
                await self.close()
                return
            if not await self._can_access_execution(user):
                await self.close()
                return
            self.group_name = f"app_execution_{self.execution_id}"
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
            await self.send_json(await self._current_state())
            logger.info(f"WebSocket 连接成功: execution_id={self.execution_id}")
        except Exception as e:
            logger.error(f"WebSocket 连接失败: {e}")
            await self.close()

    @database_sync_to_async
    def _can_access_execution(self, user):
        from .access import scope_executions
        from .models import AppTestExecution
        return scope_executions(
            AppTestExecution.objects.filter(id=self.execution_id), user
        ).exists()

    @database_sync_to_async
    def _current_state(self):
        from .access import scope_executions
        from .models import AppTestExecution

        execution = scope_executions(
            AppTestExecution.objects.filter(id=self.execution_id),
            self.scope.get('user'),
        ).first()
        if execution is None:
            return {
                'type': 'execution_update',
                'execution_id': int(self.execution_id),
                'status': 'NOT_FOUND',
                'snapshot': True,
            }

        return {
            'type': 'execution_update',
            'execution_id': execution.id,
            'status': execution.status,
            'result': execution.result,
            'progress': execution.progress,
            'message': None,
            'report_path': bool(execution.report_path),
            'finished_at': execution.finished_at.isoformat() if execution.finished_at else None,
            'total_steps': execution.total_steps,
            'passed_steps': execution.passed_steps,
            'failed_steps': execution.failed_steps,
            'snapshot': True,
        }

    async def disconnect(self, close_code):
        try:
            if hasattr(self, 'group_name'):
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
                logger.info(f"WebSocket 断开: execution_id={self.execution_id}, code={close_code}")
        except Exception as e:
            logger.error(f"WebSocket 断开处理失败: {e}")

    async def execution_update(self, event):
        try:
            await self.send_json(event)
        except Exception as e:
            logger.error(f"WebSocket 推送消息失败: {e}")
