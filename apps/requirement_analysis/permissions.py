from rest_framework.permissions import BasePermission, SAFE_METHODS

from apps.projects.models import ProjectMember
from apps.requirement_analysis.models import TestCaseGenerationTask


WRITABLE_PROJECT_ROLES = ('owner', 'admin', 'developer', 'tester')
READ_ONLY_TASK_ACTIONS = {'issue_stream_ticket', 'submission'}


def user_can_write_project(user, project):
    if not user or not getattr(user, 'is_authenticated', False) or project is None:
        return False
    if user.is_staff or user.is_superuser or project.owner_id == user.pk:
        return True
    return ProjectMember.objects.filter(
        project_id=project.pk,
        user_id=user.pk,
        role__in=WRITABLE_PROJECT_ROLES,
    ).exists()


def user_can_write_generation_task(user, task):
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_staff or user.is_superuser:
        return True
    if task.project_id:
        return user_can_write_project(user, task.project)
    return task.created_by_id == user.pk


class GenerationTaskPermission(BasePermission):
    """Keep project membership reads separate from generation-task writes."""

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False):
            return False

        action = getattr(view, 'action', None)
        if action == 'create' or action == 'generate' or action in READ_ONLY_TASK_ACTIONS:
            # Generation validates project IDs and checks project writes inside the action.
            return True
        if request.method in SAFE_METHODS:
            return True

        task_id = view.kwargs.get('task_id') or view.kwargs.get('pk')
        if not task_id:
            return True
        task = TestCaseGenerationTask.objects.select_related('project').filter(task_id=task_id).first()
        return True if task is None else user_can_write_generation_task(user, task)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS or getattr(view, 'action', None) in READ_ONLY_TASK_ACTIONS:
            return True
        return user_can_write_generation_task(request.user, obj)
