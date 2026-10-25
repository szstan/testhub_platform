from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ApiProjectViewSet, ApiCollectionViewSet, ApiRequestViewSet,
    EnvironmentViewSet, RequestHistoryViewSet, TestSuiteViewSet,
    TestSuiteRequestViewSet, TestExecutionViewSet, UserViewSet,
    ScheduledTaskViewSet, TaskExecutionLogViewSet, NotificationLogViewSet,
    TaskNotificationSettingViewSet, OperationLogViewSet,
    ApiDashboardViewSet, AIServiceConfigViewSet
)
from .contracts_views import OpenApiContractSpecViewSet

router = DefaultRouter()
router.register(r'dashboard', ApiDashboardViewSet, basename='dashboard')
router.register(r'projects', ApiProjectViewSet)
router.register(r'collections', ApiCollectionViewSet)
router.register(r'requests', ApiRequestViewSet)
router.register(r'environments', EnvironmentViewSet)
router.register(r'histories', RequestHistoryViewSet)
router.register(r'test-suites', TestSuiteViewSet)
router.register(r'test-suite-requests', TestSuiteRequestViewSet)
router.register(r'test-executions', TestExecutionViewSet)
router.register(r'users', UserViewSet)
router.register(r'scheduled-tasks', ScheduledTaskViewSet, basename='scheduledtask')
router.register(r'task-execution-logs', TaskExecutionLogViewSet, basename='taskexecutionlog')
router.register(r'notification-logs', NotificationLogViewSet)
router.register(r'task-notification-settings', TaskNotificationSettingViewSet)
router.register(r'operation-logs', OperationLogViewSet)
router.register(r'ai-service-configs', AIServiceConfigViewSet, basename='aiserviceconfig')
router.register(r'openapi-contracts', OpenApiContractSpecViewSet, basename='openapi-contract')

urlpatterns = [
    path('api-testing/', include(router.urls)),
]
