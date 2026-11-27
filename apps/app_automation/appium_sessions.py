"""Database-fenced ownership and operation dispatch for Appium sessions."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from .access import package_action_allowed, project_accessible
from .appium_models import AppiumOperation, AppiumSession
from .appium_runtime import (
    AppiumConfigurationError,
    AppiumInputError,
    build_capabilities,
    endpoint_for_device,
    system_port_for_device,
    validate_native_locator,
)
from .constants import DeviceStatus, ElementType
from .device_pool import DeviceLeaseError, validate_device_lease
from .execution_admission import assert_device_execution_idle
from .execution_dispatch import execution_user_matches
from .models import AppDevice, AppElement, AppPackage, AppProject, AppTestExecution

ACTIVE_SESSION_STATUSES = ("starting", "active", "closing", "unknown")
ACTIVE_OPERATION_STATUSES = ("queued", "running")
APP_INTERACTIVE_QUEUE = "app_interactive"
OPERATION_RESULT_TTL_SECONDS = 120
CLOSE_REPUBLISH_INTERVAL = timedelta(seconds=30)
logger = logging.getLogger(__name__)


def _result_cache_key(operation_id):
    return f"appium-result:{operation_id}"


def _cache_public_result(operation_id, result):
    try:
        cache.set(_result_cache_key(operation_id), result, timeout=OPERATION_RESULT_TTL_SECONDS)
    except Exception as exc:
        logger.warning("Appium result cache unavailable (error_type=%s)", type(exc).__name__)


class AppiumRequestError(ValueError):
    def __init__(self, detail, *, status_code=400):
        super().__init__(detail)
        self.status_code = status_code


def normalize_request_key(value):
    if not isinstance(value, str) or len(value) > 64:
        raise AppiumRequestError("A valid idempotency_key is required")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise AppiumRequestError("A valid idempotency_key is required") from None


def _require_identity(user, expected_user_id):
    if expected_user_id is None or not execution_user_matches(user, expected_user_id):
        raise AppiumRequestError("The signed-in account changed; reopen the recorder", status_code=403)


def _owned_project_package(user, project_id, package_id):
    try:
        project = AppProject.objects.get(pk=project_id)
    except (AppProject.DoesNotExist, TypeError, ValueError):
        raise AppiumRequestError("The APP project is unavailable", status_code=404) from None
    if not project_accessible(user, project):
        raise AppiumRequestError("The APP project is unavailable", status_code=404)
    if not project.appium_enabled:
        raise AppiumRequestError("Appium is not enabled for this project", status_code=409)
    try:
        package = AppPackage.objects.get(pk=package_id)
    except (AppPackage.DoesNotExist, TypeError, ValueError):
        raise AppiumRequestError("The application package is unavailable", status_code=404) from None
    if not package_action_allowed(user, package, project):
        raise AppiumRequestError("The application package is unavailable", status_code=404)
    return project, package


def _assert_lease(user, device, lease_token):
    if not isinstance(lease_token, str) or not lease_token or len(lease_token) > 1024:
        raise AppiumRequestError("The device lease is invalid", status_code=403)
    if not device.lease_token_hash:
        raise AppiumRequestError("An explicit token-fenced device lease is required", status_code=403)
    try:
        leased = validate_device_lease(device.pk, user, lease_token)
    except DeviceLeaseError:
        raise AppiumRequestError("The device lease is invalid, expired, or not owned by this user", status_code=403) from None
    if leased.lease_generation != device.lease_generation:
        raise AppiumRequestError("The device lease changed; refresh and retry", status_code=409)
    if device.maintenance_mode or device.status == DeviceStatus.OFFLINE:
        raise AppiumRequestError("The device is unavailable", status_code=409)


def _existing_start(device, user, request_key, project_id, package_id):
    session = AppiumSession.objects.filter(
        device=device, user=user, request_key=request_key,
    ).first()
    if session is None:
        return None
    if session.project_id != project_id or session.package_id != package_id:
        raise AppiumRequestError("The idempotency key was used for another session request", status_code=409)
    operation = session.operations.filter(action="start").order_by("created_at").first()
    if operation is None:
        raise AppiumRequestError("The session start is awaiting recovery", status_code=409)
    return session, operation


def reserve_interactive_session(
    *, user, device_id, lease_token, expected_user_id, project_id, package_id, request_key,
):
    """Persist lease/action/session evidence atomically before queuing device I/O."""
    _require_identity(user, expected_user_id)
    request_key = normalize_request_key(request_key)
    try:
        device = AppDevice.objects.get(pk=device_id)
    except (AppDevice.DoesNotExist, TypeError, ValueError):
        raise AppiumRequestError("The device is unavailable", status_code=404) from None
    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().get(pk=device.pk)
        existing = _existing_start(device, user, request_key, project_id, package_id)
        if existing is not None:
            return existing[0], existing[1], False
        _assert_lease(user, device, lease_token)
        project, package = _owned_project_package(user, project_id, package_id)
        try:
            assert_device_execution_idle(device.pk)
        except DeviceLeaseError:
            raise AppiumRequestError("The device is busy or awaiting recovery", status_code=409) from None
        if (
            device.device_action_pending
            or AppiumSession.objects.filter(device=device, status__in=ACTIVE_SESSION_STATUSES).exists()
        ):
            raise AppiumRequestError("The device is busy or awaiting recovery", status_code=409)
        try:
            endpoint = endpoint_for_device(device)
            system_port = system_port_for_device(device)
            capabilities = build_capabilities(device, package, system_port)
        except AppiumConfigurationError as exc:
            raise AppiumRequestError(str(exc), status_code=503) from None

        device.device_action_pending = True
        device.device_action_epoch += 1
        device.save(update_fields=["device_action_pending", "device_action_epoch", "updated_at"])
        session = AppiumSession.objects.create(
            kind="interactive",
            device=device,
            user=user,
            project=project,
            package=package,
            lease_generation=device.lease_generation,
            device_action_epoch=device.device_action_epoch,
            system_port=system_port,
            appium_endpoint=endpoint,
            capabilities=capabilities,
            status="starting",
            request_key=request_key,
        )
        operation = AppiumOperation.objects.create(
            session=session,
            user=user,
            lease_generation=device.lease_generation,
            device_action_epoch=device.device_action_epoch,
            action="start",
            request_key=request_key,
        )
    return session, operation, True


def _native_element_for_session(session, element_id):
    if type(element_id) is not int or element_id <= 0:
        raise AppiumRequestError("A valid native element_id is required")
    element = AppElement.objects.filter(
        pk=element_id,
        project_id=session.project_id,
        element_type=ElementType.NATIVE,
        is_active=True,
    ).first()
    if element is None:
        raise AppiumRequestError("The native element is unavailable in this project", status_code=404)
    config = element.config if isinstance(element.config, dict) else {}
    if config.get("package_name") != session.package.package_name:
        raise AppiumRequestError("The native element belongs to another application", status_code=404)
    try:
        strategy, value = validate_native_locator(config.get("strategy"), config.get("value"))
    except AppiumInputError:
        raise AppiumRequestError("The native element definition is invalid", status_code=409) from None
    container = config.get("container")
    if container is not None:
        if not isinstance(container, dict) or set(container) != {"strategy", "value"}:
            raise AppiumRequestError("The native element definition is invalid", status_code=409)
        try:
            container = dict(zip(("strategy", "value"), validate_native_locator(
                container.get("strategy"), container.get("value"),
            )))
        except AppiumInputError:
            raise AppiumRequestError("The native element definition is invalid", status_code=409) from None
    locator = {
        "package_name": session.package.package_name,
        "strategy": strategy,
        "value": value,
        "container": container,
    }
    return element, locator


def _draft_locator(session, candidate):
    def selector(value, *, allow_container=False):
        keys = {"strategy", "value"} | ({"container"} if allow_container else set())
        if not isinstance(value, dict) or set(value) - keys or not {"strategy", "value"} <= set(value):
            raise AppiumRequestError("The Android locator is invalid")
        try:
            strategy, query = validate_native_locator(value["strategy"], value["value"])
        except (AppiumInputError, TypeError):
            raise AppiumRequestError("The Android locator is invalid") from None
        return {"strategy": strategy, "value": query}

    locator = selector(candidate, allow_container=True)
    locator["package_name"] = session.package.package_name
    locator["container"] = selector(candidate["container"]) if candidate.get("container") is not None else None
    return locator


def _normalize_operation_payload(session, action, payload):
    if not isinstance(payload, dict):
        raise AppiumRequestError("The operation payload is invalid")
    if action in {"screenshot", "tree", "close"}:
        if payload:
            raise AppiumRequestError("This operation does not accept additional fields")
        return {}
    if action == "locate" and set(payload) == {"locator"}:
        return {"locator": _draft_locator(session, payload["locator"])}
    if action not in {"locate", "click"} or set(payload) != {"element_id"}:
        raise AppiumRequestError("The requested Appium operation is not supported")
    element, locator = _native_element_for_session(session, payload.get("element_id"))
    return {"element_id": element.pk, "locator": locator}


def _existing_close_receipt(user, session_id, request_key):
    operations = AppiumOperation.objects.select_related("session").filter(
        session_id=session_id, session__kind="interactive", session__user=user, user=user,
    )
    requested = operations.filter(request_key=request_key).first()
    if requested is not None:
        if requested.action != "close":
            raise AppiumRequestError("The idempotency key was used for another operation", status_code=409)
        if requested.status != "failed":
            return requested

    # An expiry worker can close the same session under a different request key.
    current = operations.filter(
        action="close",
        lease_generation=F("session__lease_generation"),
        device_action_epoch=F("session__device_action_epoch"),
    ).filter(
        Q(session__status="closed", status="succeeded")
        | Q(session__status="closing", status__in=ACTIVE_OPERATION_STATUSES)
        | Q(session__status__in=("closing", "unknown"), status="unknown")
    ).order_by("-created_at", "-pk").first()
    return current or requested


def reserve_session_operation(
    *, user, session_id, lease_token, expected_user_id, request_key, action, payload=None,
):
    """Persist an idempotent operation under the device/session lease fences."""
    _require_identity(user, expected_user_id)
    request_key = normalize_request_key(request_key)
    if action not in {"screenshot", "tree", "locate", "click", "close"}:
        raise AppiumRequestError("The requested Appium operation is not supported")
    confirm_close = action == "close" and not payload
    if confirm_close:
        receipt = _existing_close_receipt(user, session_id, request_key)
        if receipt is not None:
            # Reading a receipt does not dispatch device work or touch a lease.
            return receipt.session, receipt, False
    session_snapshot = AppiumSession.objects.filter(pk=session_id, user=user).values(
        "device_id",
    ).first()
    if session_snapshot is None:
        raise AppiumRequestError("The Appium session is unavailable", status_code=404)

    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().filter(pk=session_snapshot["device_id"]).first()
        session = AppiumSession.objects.select_for_update().filter(pk=session_id, user=user).first()
        if confirm_close and session is not None:
            # A worker may have reserved a close while we waited for the device lock.
            receipt = _existing_close_receipt(user, session_id, request_key)
            if receipt is not None:
                return receipt.session, receipt, False
        if device is None or session is None:
            raise AppiumRequestError("The Appium session is unavailable", status_code=404)
        existing = session.operations.filter(request_key=request_key).first()
        if existing is not None:
            if existing.action != action:
                raise AppiumRequestError("The idempotency key was used for another operation", status_code=409)
            _assert_lease(user, device, lease_token)
            if action in {"locate", "click"}:
                if not isinstance(payload, dict):
                    raise AppiumRequestError("The idempotency key was used for another operation", status_code=409)
                if set(payload) == {"locator"} and action == "locate":
                    equivalent = existing.payload == _normalize_operation_payload(session, action, payload)
                else:
                    equivalent = set(payload) == {"element_id"} and existing.payload.get("element_id") == payload["element_id"]
                if not equivalent:
                    raise AppiumRequestError("The idempotency key was used for another operation", status_code=409)
            elif payload:
                raise AppiumRequestError("The idempotency key was used for another operation", status_code=409)
            return session, existing, False
        normalized_payload = _normalize_operation_payload(session, action, payload or {})

        _assert_lease(user, device, lease_token)
        if (
            session.kind != "interactive"
            or session.device_id != device.pk
            or session.user_id != user.pk
            or session.lease_generation != device.lease_generation
            or session.device_action_epoch != device.device_action_epoch
            or not device.device_action_pending
        ):
            raise AppiumRequestError("The Appium session no longer owns this device lease", status_code=409)
        if action != "close" and (not project_accessible(user, session.project) or not session.project.appium_enabled):
            raise AppiumRequestError("The APP project is no longer available", status_code=404)
        if action != "close" and not package_action_allowed(user, session.package, session.project):
            raise AppiumRequestError("The application package is no longer available", status_code=404)
        if session.status != "active":
            raise AppiumRequestError("The Appium session is not active or requires recovery", status_code=409)
        if session.operations.filter(status__in=ACTIVE_OPERATION_STATUSES).exists():
            raise AppiumRequestError("Another Appium operation is still in progress", status_code=409)
        if action == "close" and not session.remote_session_id:
            raise AppiumRequestError("The Appium session has no confirmed remote identifier", status_code=409)
        operation = AppiumOperation.objects.create(
            session=session,
            user=user,
            lease_generation=session.lease_generation,
            device_action_epoch=session.device_action_epoch,
            action=action,
            request_key=request_key,
            payload=normalized_payload,
        )
        if action == "close":
            session.status = "closing"
            session.save(update_fields=["status", "updated_at"])
    return session, operation, True


def _settle_failed_publish(operation, attempted_at):
    snapshot = AppiumSession.objects.filter(pk=operation.session_id).values("device_id").first()
    if snapshot is None:
        return
    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().filter(pk=snapshot["device_id"]).first()
        session = AppiumSession.objects.select_for_update().filter(pk=operation.session_id).first()
        current = AppiumOperation.objects.select_for_update().filter(
            pk=operation.pk, session_id=operation.session_id, status="queued", dispatched_at=attempted_at,
        ).first()
        if session is None or current is None:
            return
        if current.action == "close":
            # A later sweep can republish this same operation until a worker claims it.
            return

        error_code = "dispatch_failed_before_device_io"
        current.mark_finished("failed", error_code=error_code)
        if (
            device is not None and session.device_id == device.pk
            and session.kind == "interactive" and session.status == "starting" and not session.remote_session_id
            and current.action == "start" and current.user_id == session.user_id
            and current.lease_generation == session.lease_generation
            and current.device_action_epoch == session.device_action_epoch
            and _session_device_fence_matches(device, session)
        ):
            session.status = "failed"
            session.failure_code = error_code
            session.closed_at = current.finished_at
            session.save(update_fields=["status", "failure_code", "closed_at", "updated_at"])
            device.device_action_pending = False
            device.save(update_fields=["device_action_pending", "updated_at"])


def dispatch_operation(operation, task, *, retry_close=False):
    """Record the publish attempt before contacting the broker, using the queued row as a fence."""
    if retry_close and operation.action != "close":
        return False
    if not retry_close and operation.dispatched_at is not None:
        return False
    attempted_at = timezone.now()
    updated = AppiumOperation.objects.filter(
        pk=operation.pk, status="queued", dispatched_at=operation.dispatched_at,
    ).update(dispatched_at=attempted_at, updated_at=attempted_at)
    if not updated:
        return False
    try:
        task.apply_async(
            args=[str(operation.pk)],
            task_id=str(operation.pk),
            queue=APP_INTERACTIVE_QUEUE,
        )
    except Exception as exc:
        logger.warning("Appium queue publish failed (error_type=%s)", type(exc).__name__)
        _settle_failed_publish(operation, attempted_at)
        return False
    return True


def poll_operation(operation):
    """Return only an authenticated operation's bounded status/result envelope."""
    if operation.status in {"queued", "running"}:
        return {"operation_id": str(operation.pk), "status": operation.status}, 202
    if operation.status == "unknown":
        return {
            "operation_id": str(operation.pk),
            "status": "unknown",
            "error": operation.error_code or "operation_outcome_unknown",
            "recovery_required": True,
        }, 200
    if operation.status == "failed":
        return {
            "operation_id": str(operation.pk),
            "status": "failed",
            "error": operation.error_code or "operation_failed",
            "recovery_required": operation.session.status == "unknown",
        }, 200

    if operation.action == "start":
        result = {"success": True, "status": "active", "session_id": str(operation.session_id)}
    elif operation.action == "close":
        lease_ended = not AppDevice.objects.filter(
            pk=operation.session.device_id,
            locked_by_id=operation.session.user_id,
            lease_generation=operation.session.lease_generation,
        ).exists()
        result = {"success": True, "status": "closed", "lease_ended": lease_ended}
    elif operation.action == "click":
        result = {
            "success": True, "action_outcome": "completed", "recovery_required": False,
            "recorded_step": {"type": "click", "element_id": operation.payload.get("element_id")},
        }
    else:
        try:
            result = cache.get(_result_cache_key(operation.pk))
        except Exception:
            result = None
    if not isinstance(result, dict):
        return {
            "operation_id": str(operation.pk),
            "status": "failed",
            "error": "operation_result_unavailable",
            "recovery_required": False,
        }, 200
    return {
        "operation_id": str(operation.pk),
        "status": operation.status,
        "result": result,
    }, 200


def fenced_session_snapshot(session_id, operation_id):
    """Claim a queued operation before I/O and return its related locked snapshots."""
    snapshot = AppiumSession.objects.filter(pk=session_id).values(
        "device_id",
    ).first()
    if snapshot is None:
        return None
    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().filter(pk=snapshot["device_id"]).first()
        session = AppiumSession.objects.select_for_update().filter(pk=session_id).select_related(
            "device", "user", "project", "package", "execution",
        ).first()
        operation = AppiumOperation.objects.select_for_update().filter(
            pk=operation_id, session_id=session_id,
        ).first()
        if device is None or session is None or operation is None:
            return None
        if operation.status == "running":
            return None
        if operation.status != "queued":
            return None
        if (
            session.device_id != device.pk
            or operation.lease_generation != session.lease_generation
            or operation.lease_generation != device.lease_generation
            or operation.device_action_epoch != session.device_action_epoch
            or operation.device_action_epoch != device.device_action_epoch
            or not device.device_action_pending
            or device.locked_by_id != session.user_id
            or session.user_id != operation.user_id
        ):
            operation.status = "unknown"
            operation.error_code = "ownership_fence_changed"
            operation.finished_at = timezone.now()
            operation.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
            return None
        if session.kind == "interactive" and _interactive_lease_expired(device, timezone.now()):
            if not _settle_expired_interactive_locked(
                device, session, timezone.now(), operation=operation, dispatch_close=True,
            ):
                return None
        if operation.action != "close":
            try:
                from .device_pool import assert_device_execution_lease
                assert_device_execution_lease(device.pk, session.user, session.lease_generation)
            except DeviceLeaseError:
                if session.kind == "interactive" and _interactive_lease_expired(device, timezone.now()):
                    _settle_expired_interactive_locked(
                        device, session, timezone.now(), operation=operation, dispatch_close=True,
                    )
                    return None
                operation.status = "unknown"
                operation.error_code = "lease_fence_changed"
                operation.finished_at = timezone.now()
                operation.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
                return None
        if operation.action != "close" and (not project_accessible(session.user, session.project) or not session.project.appium_enabled):
            operation.status = "failed"
            operation.error_code = "project_access_revoked"
            operation.finished_at = timezone.now()
            operation.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
            if operation.action == "start":
                session.status = "failed"
                session.failure_code = operation.error_code
                session.save(update_fields=["status", "failure_code", "updated_at"])
                device.device_action_pending = False
                device.save(update_fields=["device_action_pending", "updated_at"])
            return None
        if operation.action != "close" and not package_action_allowed(session.user, session.package, session.project):
            operation.status = "failed"
            operation.error_code = "package_access_revoked"
            operation.finished_at = timezone.now()
            operation.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
            if operation.action == "start":
                session.status = "failed"
                session.failure_code = operation.error_code
                session.save(update_fields=["status", "failure_code", "updated_at"])
                device.device_action_pending = False
                device.save(update_fields=["device_action_pending", "updated_at"])
            return None
        if operation.action == "start":
            if session.status != "starting":
                return None
        elif operation.action == "close":
            if session.status != "closing" or not session.remote_session_id:
                operation.status = "unknown"
                operation.error_code = "session_close_fence_changed"
                operation.finished_at = timezone.now()
                operation.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
                session.status = "unknown"
                session.failure_code = operation.error_code
                session.save(update_fields=["status", "failure_code", "updated_at"])
                return None
        elif session.status != "active":
            return None
        if operation.action == "close":
            if session.operations.filter(status="unknown").exclude(pk=operation.pk).exists():
                operation.status = "unknown"
                operation.error_code = "prior_operation_outcome_unknown"
                operation.finished_at = timezone.now()
                operation.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
                session.status = "unknown"
                session.failure_code = operation.error_code
                session.save(update_fields=["status", "failure_code", "updated_at"])
                return None
            if session.operations.filter(status__in=ACTIVE_OPERATION_STATUSES).exclude(pk=operation.pk).exists():
                return None
        operation.status = "running"
        operation.dispatched_at = operation.dispatched_at or timezone.now()
        operation.save(update_fields=["status", "dispatched_at", "updated_at"])
        return session, operation, device


def _interactive_lease_expired(device, cutoff):
    return bool(device.lease_expires_at and device.lease_expires_at <= cutoff)


def _session_device_fence_matches(device, session):
    return (
        device.locked_by_id == session.user_id
        and device.lease_generation == session.lease_generation
        and device.device_action_epoch == session.device_action_epoch
        and device.device_action_pending
    )


def _settle_expired_interactive_start_locked(device, session, cutoff, operation=None):
    """Expire an unclaimed start, retaining its fence after claimed I/O."""
    if session.status != "starting" or not _interactive_lease_expired(device, cutoff):
        return False
    if operation is None or operation.action != "start":
        operation = AppiumOperation.objects.select_for_update().filter(
            session=session, action="start",
        ).order_by("created_at").first()
    now = timezone.now()
    if operation is None or session.remote_session_id:
        error_code = "start_expiry_outcome_unknown"
        if operation is not None and operation.status == "queued":
            operation.status = "unknown"
            operation.error_code = error_code
            operation.finished_at = now
            operation.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
        session.status = "unknown"
        session.failure_code = error_code
        session.save(update_fields=["status", "failure_code", "updated_at"])
        return True

    if operation.status == "queued":
        status = "failed"
        error_code = "start_expired_before_device_io"
        clear_action_marker = True
        operation.status = status
        operation.error_code = error_code
        operation.finished_at = now
        operation.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
    elif operation.status == "failed":
        status = "failed"
        error_code = operation.error_code or "appium_start_failed"
        clear_action_marker = True
    else:
        status = "unknown"
        error_code = operation.error_code or "start_expiry_outcome_unknown"
        clear_action_marker = False
        if operation.status == "running":
            operation.status = "unknown"
            operation.error_code = error_code
            operation.finished_at = now
            operation.save(update_fields=["status", "error_code", "finished_at", "updated_at"])

    session.status = status
    session.failure_code = error_code
    if status == "failed":
        session.closed_at = now
        session.save(update_fields=["status", "failure_code", "closed_at", "updated_at"])
    else:
        session.save(update_fields=["status", "failure_code", "updated_at"])

    owns_action_marker = (
        operation.user_id == session.user_id
        and operation.lease_generation == session.lease_generation
        and operation.device_action_epoch == session.device_action_epoch
        and _session_device_fence_matches(device, session)
    )
    if clear_action_marker and owns_action_marker:
        device.device_action_pending = False
        device.save(update_fields=["device_action_pending", "updated_at"])
    return True


def _settle_expired_interactive_locked(device, session, cutoff, operation=None, *, dispatch_close):
    """Settle only unclaimed work; never release a claimed or uncertain remote outcome."""
    if session.kind != "interactive" or not _interactive_lease_expired(device, cutoff):
        return operation is not None
    if session.status == "starting":
        _settle_expired_interactive_start_locked(device, session, cutoff, operation)
        return False
    if session.status not in {"active", "closing"} or not _session_device_fence_matches(device, session):
        return False

    operations = list(AppiumOperation.objects.select_for_update().filter(
        session=session,
    ).order_by("created_at"))
    invalid_fence = next((item for item in operations if (
        item.user_id != session.user_id
        or item.lease_generation != session.lease_generation
        or item.device_action_epoch != session.device_action_epoch
    )), None)
    if invalid_fence is not None:
        invalid_fence.status = "unknown"
        invalid_fence.error_code = "ownership_fence_changed"
        invalid_fence.finished_at = timezone.now()
        invalid_fence.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
        session.status = "unknown"
        session.failure_code = invalid_fence.error_code
        session.save(update_fields=["status", "failure_code", "updated_at"])
        return False

    now = timezone.now()
    for item in operations:
        if item.status == "queued" and item.action != "close":
            item.status = "failed"
            item.error_code = "operation_expired_before_device_io"
            item.finished_at = now
            item.save(update_fields=["status", "error_code", "finished_at", "updated_at"])

    uncertain = next((item for item in operations if item.status == "unknown"), None)
    if uncertain is not None:
        for item in operations:
            if item.status == "queued" and item.action == "close":
                item.status = "unknown"
                item.error_code = "prior_operation_outcome_unknown"
                item.finished_at = now
                item.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
        session.status = "unknown"
        session.failure_code = uncertain.error_code or "prior_operation_outcome_unknown"
        session.save(update_fields=["status", "failure_code", "updated_at"])
        return False
    if any(item.status == "running" for item in operations):
        return False
    if not session.remote_session_id:
        session.status = "unknown"
        session.failure_code = "session_close_identifier_unavailable"
        session.save(update_fields=["status", "failure_code", "updated_at"])
        return False

    queued_closes = [item for item in operations if item.action == "close" and item.status == "queued"]
    if queued_closes:
        close_operation = queued_closes[0]
        for duplicate in queued_closes[1:]:
            duplicate.status = "failed"
            duplicate.error_code = "duplicate_close_operation"
            duplicate.finished_at = now
            duplicate.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
    elif session.status == "active":
        close_operation = AppiumOperation.objects.create(
            session=session,
            user_id=session.user_id,
            lease_generation=session.lease_generation,
            device_action_epoch=session.device_action_epoch,
            action="close",
            request_key=str(uuid.uuid4()),
        )
    else:
        session.status = "unknown"
        session.failure_code = "close_operation_missing"
        session.save(update_fields=["status", "failure_code", "updated_at"])
        return False

    if session.status != "closing":
        session.status = "closing"
        session.save(update_fields=["status", "updated_at"])

    can_claim_close = operation is not None and operation.pk == close_operation.pk
    if dispatch_close and not can_claim_close and (
        close_operation.dispatched_at is None
        or close_operation.dispatched_at <= device.lease_expires_at
        or close_operation.dispatched_at <= cutoff - CLOSE_REPUBLISH_INTERVAL
    ):
        transaction.on_commit(lambda item=close_operation: _dispatch_expired_close(item))
    return can_claim_close


def _dispatch_expired_close(operation):
    from .appium_tasks import close_appium_session_task

    dispatch_operation(operation, close_appium_session_task, retry_close=True)


def close_expired_interactive_sessions(*, max_sessions=50):
    """Settle expired interactive work and dispatch one safe teardown operation."""
    cutoff = timezone.now()
    operations = AppiumOperation.objects.filter(session_id=OuterRef("pk"))
    first_queued_close = operations.filter(action="close", status="queued").order_by("created_at", "pk")
    owned_idle_session = Q(
        device__maintenance_mode=False,
        device__device_action_pending=True,
        device__locked_by_id=F("user_id"),
        device__lease_generation=F("lease_generation"),
        device__device_action_epoch=F("device_action_epoch"),
        has_running_operation=False,
    )
    close_is_due = (
        Q(last_close_publish__isnull=True)
        | Q(last_close_publish__lte=F("device__lease_expires_at"))
        | Q(last_close_publish__lte=cutoff - CLOSE_REPUBLISH_INTERVAL)
    )
    expired = (
        AppiumSession.objects.filter(
            kind="interactive", status__in=("starting", "active", "closing"),
            device__lease_expires_at__lte=cutoff,
        )
        .alias(
            has_running_operation=Exists(operations.filter(status="running")),
            last_close_publish=Subquery(first_queued_close.values("dispatched_at")[:1]),
        )
        .filter(Q(status="starting") | (owned_idle_session & close_is_due))
        # A close we just republished goes behind older work at the next sweep.
        .order_by(Coalesce("last_close_publish", "created_at"), "pk")
        .values_list("pk", "device_id")[:max_sessions]
    )
    for session_id, device_id in list(expired):
        with transaction.atomic(durable=True):
            device = AppDevice.objects.select_for_update().filter(pk=device_id).first()
            session = AppiumSession.objects.select_for_update().filter(
                pk=session_id, device_id=device_id, kind="interactive",
            ).first()
            if device is None or session is None or not _interactive_lease_expired(device, cutoff):
                continue
            if session.status == "starting":
                _settle_expired_interactive_start_locked(device, session, cutoff)
                continue
            if (
                session.status not in {"active", "closing"}
                or device.maintenance_mode
                or not _session_device_fence_matches(device, session)
            ):
                continue
            _settle_expired_interactive_locked(
                device, session, cutoff, dispatch_close=True,
            )


def resolve_operation_element(session, operation):
    if operation.action == "locate" and "element_id" not in operation.payload:
        locator = dict(operation.payload.get("locator", {}))
        locator.pop("package_name", None)
        return _draft_locator(session, locator)
    _element, locator = _native_element_for_session(session, operation.payload.get("element_id"))
    if locator != operation.payload.get("locator"):
        raise AppiumRequestError("The native element definition changed after operation submission", status_code=409)
    return locator


def finish_operation(operation_id, session_id, *, status, result=None, error_code=""):
    """Persist completion only under the original session/device action epoch."""
    snapshot = AppiumSession.objects.filter(pk=session_id).values("device_id").first()
    if snapshot is None:
        return False
    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().filter(pk=snapshot["device_id"]).first()
        session = AppiumSession.objects.select_for_update().filter(pk=session_id).first()
        operation = AppiumOperation.objects.select_for_update().filter(
            pk=operation_id, session_id=session_id,
        ).first()
        if device is None or session is None or operation is None:
            return False
        if operation.status != "running":
            return False
        owns_fence = (
            device.locked_by_id == session.user_id
            and device.lease_generation == session.lease_generation
            and device.device_action_epoch == session.device_action_epoch
            and device.device_action_pending
            and operation.lease_generation == session.lease_generation
            and operation.device_action_epoch == session.device_action_epoch
        )
        if not owns_fence:
            operation.status = "unknown"
            operation.error_code = "ownership_fence_changed"
            operation.finished_at = timezone.now()
            operation.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
            return False
        operation.status = status
        operation.error_code = error_code
        operation.finished_at = timezone.now()
        operation.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
        if status == "succeeded" and operation.action in {"screenshot", "tree", "locate"}:
            transaction.on_commit(lambda: _cache_public_result(operation.pk, result))
        if operation.action == "start" and status == "succeeded":
            session.status = "active"
            session.remote_session_id = result.get("remote_session_id", "") if isinstance(result, dict) else ""
            session.capabilities = result.get("capabilities", session.capabilities) if isinstance(result, dict) else session.capabilities
            session.failure_code = ""
            session.save(update_fields=["status", "remote_session_id", "capabilities", "failure_code", "updated_at"])
        elif operation.action == "close" and status == "succeeded":
            session.status = "closed"
            session.closed_at = timezone.now()
            session.remote_session_id = ""
            session.failure_code = ""
            session.save(update_fields=["status", "closed_at", "remote_session_id", "failure_code", "updated_at"])
            device.device_action_pending = False
            device.save(update_fields=["device_action_pending", "updated_at"])
        elif status == "unknown":
            session.status = "unknown"
            session.failure_code = error_code or "operation_outcome_unknown"
            session.save(update_fields=["status", "failure_code", "updated_at"])
        elif operation.action == "close" and status == "failed" and session.status == "closing":
            session.status = "active"
            session.failure_code = ""
            session.save(update_fields=["status", "failure_code", "updated_at"])
        elif operation.action == "start" and status == "failed":
            session.status = "failed"
            session.failure_code = error_code
            session.closed_at = timezone.now()
            session.save(update_fields=["status", "failure_code", "closed_at", "updated_at"])
            device.device_action_pending = False
            device.save(update_fields=["device_action_pending", "updated_at"])
        return True
