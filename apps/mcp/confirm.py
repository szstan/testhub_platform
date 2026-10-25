"""危险操作 preview → confirm 两段式机制。

- preview：创建 McpPendingConfirm（status=pending），签发带时间戳的令牌
- confirm：TimestampSigner 无状态校验（5 分钟 max_age）+ DB 状态双重验证，
  通过后执行动作并置为 consumed（单次消费）
- 人工审批模式（MCP_HUMAN_APPROVAL=True）：confirm 不直接执行，
  标记 awaiting_human 等待控制台批准，Agent 轮询 get_approval_status 取结果
- 监控页批准：服务端直接触发动作执行（approved）
- 监控页拒绝：令牌作废（rejected），后续 confirm 返回明确错误

令牌不落 Redis，用 django.core.signing 实现无状态 + 防篡改，
DB 记录承载状态机与监控可视化，两者互补。
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.core import signing
from django.utils import timezone

from .actions import ACTION_REGISTRY, McpActionError, build_preview
from .models import CONFIRM_TOKEN_TTL_SECONDS, McpPendingConfirm

logger = logging.getLogger(__name__)

_SALT = 'apps.mcp.confirm-token'


class ConfirmError(Exception):
    """confirm 校验失败（面向 Agent 的可读错误）。"""


def _human_approval_enabled() -> bool:
    return bool(getattr(settings, 'MCP_HUMAN_APPROVAL', False))


def _is_privileged_operator(operator) -> bool:
    return bool(
        operator is not None
        and (getattr(operator, 'is_staff', False) or getattr(operator, 'is_superuser', False))
    )


def notify_pending_approval(pending: McpPendingConfirm):
    """人工审批提醒：复用 monitor 模块已启用的通知渠道。

    未配置任何渠道或发送失败均静默降级（仅记日志），不阻断主流程。
    """
    try:
        from apps.monitor.models import NotificationChannel
        from apps.monitor.utils.notifiers import send_via_channel

        channels = NotificationChannel.objects.filter(enabled=True)
        if not channels.exists():
            logger.debug('MCP 审批通知跳过：未配置启用的通知渠道')
            return
        message = (
            f'【TestHub MCP 危险操作待审批】\n'
            f'发起人：{pending.user.username}\n'
            f'操作：{pending.tool_name}\n'
            f'影响：{pending.preview}\n'
            f'过期时间：{timezone.localtime(pending.expires_at):%Y-%m-%d %H:%M:%S}\n'
            f'请到「MCP 控制台 → 待确认」批准或拒绝。'
        )
        for channel in channels:
            ok, detail = send_via_channel(channel, message, subject='MCP 危险操作待审批')
            if not ok:
                logger.warning('MCP 审批通知发送失败 channel=%s: %s', channel.name, detail)
    except Exception as exc:  # noqa: BLE001 - 通知失败不阻断审批流程
        logger.debug('MCP 审批通知发送异常: %s', exc)


def create_preview(tool_name: str, arguments: dict, user) -> dict:
    """创建待确认记录并返回 preview 结果（供 preview_* 工具使用）。"""
    from django.core.cache import cache
    now = timezone.now()
    rate_key = f'mcp:preview:rate:{user.id}'
    if not getattr(settings, 'DEBUG', False) and not getattr(settings, 'TESTING', False) and not cache.add(rate_key, '1', timeout=1):
        raise ConfirmError('危险操作预览请求过于频繁，请稍后重试')
    active_count = McpPendingConfirm.objects.filter(
        user=user, status='pending', expires_at__gt=now
    ).count()
    if not getattr(settings, 'DEBUG', False) and not getattr(settings, 'TESTING', False) and active_count >= 20:
        raise ConfirmError('待确认危险操作已达到上限，请先处理或等待过期')
    McpPendingConfirm.objects.filter(
        status='expired', expires_at__lt=now - timedelta(days=1)
    ).delete()
    preview_text = build_preview(tool_name, arguments, user)
    pending = McpPendingConfirm.objects.create(
        user=user,
        tool_name=tool_name,
        arguments=arguments,
        preview=preview_text,
        status='pending',
        expires_at=McpPendingConfirm.default_expires_at(),
    )
    signer = signing.TimestampSigner(salt=_SALT)
    token = signer.sign(str(pending.id))
    if _human_approval_enabled():
        notify_pending_approval(pending)
    return {
        'preview': preview_text,
        'confirm_token': token,
        'confirm_tool': tool_name,
        'expires_in_seconds': CONFIRM_TOKEN_TTL_SECONDS,
    }


def verify_pending(confirm_token: str, user) -> McpPendingConfirm:
    """校验 confirm 令牌并返回待确认记录；任何异常抛 ConfirmError。"""
    if not confirm_token:
        raise ConfirmError('缺少 confirm_token')
    try:
        signed_value = signing.TimestampSigner(salt=_SALT).unsign(
            confirm_token, max_age=CONFIRM_TOKEN_TTL_SECONDS)
    except signing.SignatureExpired:
        raise ConfirmError('confirm_token 已过期（5 分钟有效），请重新调用 preview') from None
    except signing.BadSignature:
        raise ConfirmError('confirm_token 无效或已被篡改') from None

    pending = McpPendingConfirm.objects.filter(id=int(signed_value)).first()
    if pending is None:
        raise ConfirmError('confirm_token 对应的操作不存在')
    if pending.user_id != user.id:
        raise ConfirmError('无权确认其他用户发起的操作')
    if pending.status == 'rejected':
        raise ConfirmError('该操作已被管理员拒绝')
    if pending.status == 'consumed':
        raise ConfirmError('该操作已被执行，不可重复确认')
    if pending.status == 'approved':
        raise ConfirmError('该操作已由管理员批准执行，无需重复确认')
    if pending.is_expired:
        pending.status = 'expired'
        pending.save(update_fields=['status', 'updated_at'])
        raise ConfirmError('该操作已过期，请重新调用 preview')
    return pending


def consume_pending(pending: McpPendingConfirm, user) -> dict:
    """Claim a pending action atomically before executing it once."""
    with transaction.atomic():
        locked = McpPendingConfirm.objects.select_for_update().get(pk=pending.pk)
        if locked.user_id != user.id:
            raise ConfirmError('无权确认其他用户发起的操作')
        if locked.status != 'pending':
            raise ConfirmError(f'该操作当前状态为 {locked.status}，不可重复执行')
        if locked.is_expired:
            locked.status = 'expired'
            locked.save(update_fields=['status', 'updated_at'])
            raise ConfirmError('该操作已过期，请重新调用 preview')
        if _human_approval_enabled():
            if not locked.awaiting_human:
                locked.awaiting_human = True
                locked.save(update_fields=['awaiting_human', 'updated_at'])
            return {
                'status': 'awaiting_approval',
                'pending_id': locked.id,
                'hint': '人工审批模式已开启：该操作已转入 TestHub「MCP 控制台」等待批准，'
                        '请稍后调用 get_approval_status 轮询审批结果',
            }
        action = ACTION_REGISTRY.get(locked.tool_name)
        if action is None:
            raise ConfirmError(f'未知的待确认操作: {locked.tool_name}')
        # Mark consumed before the external side effect so concurrent confirms cannot duplicate it.
        locked.status = 'consumed'
        locked.result = {'status': 'executing'}
        locked.save(update_fields=['status', 'result', 'updated_at'])
        arguments = dict(locked.arguments or {})

    try:
        result = action(arguments, user)
    except Exception as e:
        logger.exception('MCP action failed tool=%s', locked.tool_name)
        McpPendingConfirm.objects.filter(pk=pending.pk).update(
            result={'status': 'failed'}, updated_at=timezone.now())
        raise ConfirmError('危险操作执行失败，请查看审计日志') from e
    McpPendingConfirm.objects.filter(pk=pending.pk).update(
        result=result or {}, updated_at=timezone.now())
    return result


def approve_pending(pending: McpPendingConfirm, operator) -> dict:
    """Claim and execute a pending action from the admin console once.

    Approval is a second-party control: only a privileged operator who is not
    the action initiator may approve it.  Keep this invariant here as well as
    in the REST view so callers cannot bypass it by invoking the service layer.
    """
    with transaction.atomic():
        locked = McpPendingConfirm.objects.select_for_update().get(pk=pending.pk)
        operator_id = getattr(operator, 'id', None)
        if operator_id is not None and locked.user_id == operator_id:
            raise ConfirmError('危险操作发起人不能批准自己的操作')
        if not _is_privileged_operator(operator):
            raise ConfirmError('仅管理员或超级管理员可以批准危险操作')
        if locked.status != 'pending' or locked.is_expired:
            raise ConfirmError(f'当前状态({locked.status})不可批准')
        action = ACTION_REGISTRY.get(locked.tool_name)
        if action is None:
            raise ConfirmError(f'未知的待确认操作: {locked.tool_name}')
        locked.status = 'consumed'
        locked.result = {'status': 'executing', 'approved_by': operator.username}
        locked.save(update_fields=['status', 'result', 'updated_at'])
        arguments = dict(locked.arguments or {})

    try:
        result = action(arguments, locked.user)
    except Exception as e:
        logger.exception('MCP approved action failed tool=%s', locked.tool_name)
        McpPendingConfirm.objects.filter(pk=pending.pk).update(
            result={'status': 'failed', 'approved_by': operator.username}, updated_at=timezone.now())
        raise ConfirmError('批准操作执行失败，请查看审计日志') from e
    McpPendingConfirm.objects.filter(pk=pending.pk).update(
        status='approved',
        result={**(result or {}), 'approved_by': operator.username},
        updated_at=timezone.now(),
    )
    return result



def reject_pending(pending, operator):
    """监控页拒绝：在行锁内条件作废令牌。"""
    with transaction.atomic():
        locked = McpPendingConfirm.objects.select_for_update().get(pk=pending.pk)
        if locked.status != 'pending':
            raise ConfirmError(f'当前状态({locked.status})不可拒绝')
        locked.status = 'rejected'
        locked.result = {'rejected_by': operator.username,
                         'rejected_at': timezone.now().isoformat()}
        locked.save(update_fields=['status', 'result', 'updated_at'])
        return locked


def _unsign_token(confirm_token: str):
    """解签 confirm 令牌；过期返回 None，无效/篡改抛 ConfirmError。"""
    if not confirm_token:
        raise ConfirmError('缺少 confirm_token')
    try:
        signed_value = signing.TimestampSigner(salt=_SALT).unsign(
            confirm_token, max_age=CONFIRM_TOKEN_TTL_SECONDS)
    except signing.SignatureExpired:
        return None
    except signing.BadSignature:
        raise ConfirmError('confirm_token 无效或已被篡改') from None
    return signed_value


def query_approval_status(confirm_token: str, user) -> dict:
    """轮询人工审批状态（get_approval_status 工具入口）。

    与 verify_pending 不同：终态（已批准/已拒绝/已过期）不抛错，
    而是返回状态与结果摘要，供 Agent 反复轮询。
    """
    signed_value = _unsign_token(confirm_token)
    if signed_value is None:
        return {'status': 'expired', 'hint': '令牌已过期（5 分钟有效），请重新调用 preview'}

    pending = McpPendingConfirm.objects.filter(id=int(signed_value)).first()
    if pending is None:
        raise ConfirmError('confirm_token 对应的操作不存在')
    if pending.user_id != user.id:
        raise ConfirmError('无权查询其他用户发起的操作')

    if pending.status in ('approved', 'consumed'):
        if (pending.result or {}).get('status') == 'failed':
            return {'status': 'failed', 'result': pending.result or {}}
        return {'status': 'approved', 'result': pending.result or {}}
    if pending.status == 'rejected':
        return {'status': 'rejected', 'result': pending.result or {}}
    if pending.is_expired:
        pending.status = 'expired'
        pending.save(update_fields=['status', 'updated_at'])
        return {'status': 'expired', 'hint': '操作已过期，请重新调用 preview'}
    remaining = int((pending.expires_at - timezone.now()).total_seconds())
    return {
        'status': 'awaiting_approval' if pending.awaiting_human else 'pending',
        'preview': pending.preview,
        'expires_in_seconds': max(remaining, 0),
        'hint': '等待人工审批，请稍后再次轮询' if pending.awaiting_human else '尚未确认执行',
    }
