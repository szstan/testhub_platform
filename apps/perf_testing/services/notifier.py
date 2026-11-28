"""压测结果通知：复用 monitor 模块已有的钉钉/企微/邮件渠道，不重复造轮子。"""
import logging

logger = logging.getLogger(__name__)


def build_message(execution):
    summary = execution.summary or {}
    sla_icon = {'PASSED': '✅ 通过', 'FAILED': '❌ 未通过'}.get(
        execution.sla_result, '➖ 未评估')
    status_text = dict(execution.STATUS_CHOICES).get(execution.status, execution.status)

    lines = [
        f'【性能测试】{execution.scenario.name}',
        f'执行编号：{execution.execution_no}',
        f'执行状态：{status_text}',
        f'SLA 判定：{sla_icon}',
        f'总请求数：{summary.get("total_requests", 0)}',
        f'TPS：{summary.get("tps", 0)}（峰值 {summary.get("peak_tps", 0)}）',
        f'平均响应：{summary.get("avg_rt", 0)} ms  P95：{summary.get("p95_rt", 0)} ms',
        f'错误率：{summary.get("error_rate", 0)}%',
        f'执行时长：{execution.duration or 0} s',
    ]
    if not summary.get('data_trustworthy', True):
        lines.append(f'⚠️ 压力机 CPU 峰值 {summary.get("peak_load_gen_cpu")}%，数据可信度存疑')

    failed_items = [d for d in (execution.sla_detail or []) if not d.get('passed')]
    if failed_items:
        lines.append('未达标项：')
        for item in failed_items[:5]:
            lines.append(f'  · {item.get("label")}：实测 {item.get("actual")} '
                         f'{item.get("comparator")} 阈值 {item.get("threshold")}')
    if execution.error_message:
        lines.append(f'错误信息：{execution.error_message[:200]}')
    return '\n'.join(lines)


def send_execution_notification(execution, channel_ids, owner_id=None):
    """Send only through currently enabled channels still owned by the saved actor."""
    if not isinstance(channel_ids, (list, tuple, set)) or not channel_ids:
        return []

    normalized_ids = []
    for value in channel_ids:
        if isinstance(value, bool):
            continue
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            continue
        if channel_id > 0 and channel_id not in normalized_ids:
            normalized_ids.append(channel_id)
    if not normalized_ids or owner_id is None:
        return []

    try:
        from django.contrib.auth import get_user_model
        from apps.monitor.access import is_monitor_admin
        from apps.monitor.models import NotificationChannel
        from apps.monitor.utils.notifiers import send_via_channel
    except ImportError as exc:
        logger.warning('通知模块不可用: %s', exc)
        return []

    owner = get_user_model().objects.filter(pk=owner_id, is_active=True).only(
        'is_staff', 'is_superuser').first()
    if owner is None:
        return []
    channels = NotificationChannel.objects.filter(
        id__in=normalized_ids, enabled=True)
    if not is_monitor_admin(owner):
        channels = channels.filter(created_by_id=owner_id)

    message = build_message(execution)
    subject = f'性能测试结果 - {execution.scenario.name} - {execution.sla_result}'
    results = []
    for channel in channels:
        try:
            ok = send_via_channel(channel, message, subject=subject)
            results.append({'channel': channel.name, 'success': bool(ok)})
        except Exception as exc:  # noqa: BLE001 - 单渠道失败不影响其他渠道
            logger.warning('渠道 %s 发送失败: %s', channel.name, exc)
            results.append({'channel': channel.name, 'success': False, 'error': str(exc)})
    return results
