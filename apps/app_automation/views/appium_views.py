"""Authenticated recorder endpoints backed by the isolated Appium task queue."""

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..appium_models import AppiumOperation
from ..appium_sessions import (
    AppiumRequestError,
    dispatch_operation,
    normalize_request_key,
    poll_operation,
    reserve_interactive_session,
    reserve_session_operation,
)
from ..execution_dispatch import execution_user_matches


IDENTITY_FIELDS = {"expected_user_id", "lease_token", "idempotency_key"}


def _identity_payload(payload):
    if not isinstance(payload, dict) or not IDENTITY_FIELDS.issubset(payload):
        raise AppiumRequestError("The session identity is incomplete")
    if set(payload) - IDENTITY_FIELDS:
        raise AppiumRequestError("The request contains unsupported fields")
    return payload["expected_user_id"], payload["lease_token"], normalize_request_key(payload["idempotency_key"])


def _accepted(session, operation):
    return {
        "session_id": str(session.pk),
        "operation_id": str(operation.pk),
        "status": operation.status,
    }


def _dispatch(task, session, operation, created):
    if created:
        dispatch_operation(operation, task)
        operation.refresh_from_db(fields=["status"])
    return Response(_accepted(session, operation), status=status.HTTP_202_ACCEPTED)


def _request_error(error):
    return Response({"detail": str(error)}, status=error.status_code)


class AppiumSessionStartView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, device_id):
        payload = request.data
        allowed = IDENTITY_FIELDS | {"project_id", "app_package_id"}
        if not isinstance(payload, dict) or set(payload) != allowed:
            return Response({"detail": "The session request is invalid"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            expected_user_id, lease_token, request_key = _identity_payload({
                key: payload[key] for key in IDENTITY_FIELDS
            })
            if not execution_user_matches(request.user, expected_user_id):
                raise AppiumRequestError("The signed-in account changed; reopen the recorder", status_code=403)
            session, operation, created = reserve_interactive_session(
                user=request.user,
                device_id=device_id,
                lease_token=lease_token,
                expected_user_id=expected_user_id,
                project_id=payload["project_id"],
                package_id=payload["app_package_id"],
                request_key=request_key,
            )
        except AppiumRequestError as exc:
            return _request_error(exc)
        except Exception:
            return Response({"detail": "The Appium session could not be reserved"}, status=503)

        from ..appium_tasks import start_appium_session_task
        return _dispatch(start_appium_session_task, session, operation, created)


class AppiumSessionOperationView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, session_id):
        payload = request.data
        allowed = IDENTITY_FIELDS | {"action", "element_id", "locator"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            return Response({"detail": "The operation request is invalid"}, status=status.HTTP_400_BAD_REQUEST)
        if not IDENTITY_FIELDS.issubset(payload) or "action" not in payload:
            return Response({"detail": "The operation identity is incomplete"}, status=status.HTTP_400_BAD_REQUEST)
        action = payload["action"]
        if action not in {"screenshot", "tree", "locate", "click"}:
            return Response({"detail": "The requested Appium operation is not supported"}, status=status.HTTP_400_BAD_REQUEST)
        operation_payload = {key: payload[key] for key in ("element_id", "locator") if key in payload}
        try:
            expected_user_id, lease_token, request_key = _identity_payload({
                key: payload[key] for key in IDENTITY_FIELDS
            })
            session, operation, created = reserve_session_operation(
                user=request.user,
                session_id=session_id,
                lease_token=lease_token,
                expected_user_id=expected_user_id,
                request_key=request_key,
                action=action,
                payload=operation_payload,
            )
        except AppiumRequestError as exc:
            return _request_error(exc)
        except Exception:
            return Response({"detail": "The Appium operation could not be reserved"}, status=503)

        from ..appium_tasks import run_appium_operation_task
        return _dispatch(run_appium_operation_task, session, operation, created)


class AppiumSessionCloseView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, session_id):
        try:
            expected_user_id, lease_token, request_key = _identity_payload(request.data)
            session, operation, created = reserve_session_operation(
                user=request.user,
                session_id=session_id,
                lease_token=lease_token,
                expected_user_id=expected_user_id,
                request_key=request_key,
                action="close",
                payload={},
            )
        except AppiumRequestError as exc:
            return _request_error(exc)
        except Exception:
            return Response({"detail": "The Appium session could not be closed"}, status=503)

        from ..appium_tasks import close_appium_session_task
        return _dispatch(close_appium_session_task, session, operation, created)


class AppiumOperationStatusView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, operation_id):
        operation = AppiumOperation.objects.filter(
            pk=operation_id, user=request.user,
        ).select_related("session").first()
        if operation is None:
            return Response({"detail": "The Appium operation is unavailable"}, status=status.HTTP_404_NOT_FOUND)
        result, http_status = poll_operation(operation)
        return Response(result, status=http_status)
