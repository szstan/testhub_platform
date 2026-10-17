"""Shared authorization helpers for performance-testing resources."""
from django.db.models import Q

from .models import PerfProject


def is_privileged(user):
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def accessible_projects(user):
    """Return projects the user may read or use."""
    if is_privileged(user):
        return PerfProject.objects.all()
    if user is None or not getattr(user, "is_authenticated", False):
        return PerfProject.objects.none()
    return PerfProject.objects.filter(
        Q(owner=user) | Q(members=user)
    ).distinct().order_by('-created_at')


def scope_by_project(queryset, user, relation="project"):
    """Scope a queryset whose project relation is given by ``relation``."""
    if is_privileged(user):
        return queryset
    if user is None or not getattr(user, "is_authenticated", False):
        return queryset.none()
    return queryset.filter(
        Q(**{f"{relation}__owner": user})
        | Q(**{f"{relation}__members": user})
    ).distinct()


def can_access_project(user, project):
    if project is None:
        return False
    if is_privileged(user):
        return True
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (project.owner_id == user.id or project.members.filter(pk=user.pk).exists())
    )


def can_manage_project(user, project):
    if project is None:
        return False
    return is_privileged(user) or (
        user is not None
        and getattr(user, "is_authenticated", False)
        and project.owner_id == user.id
    )


def can_access_execution(user, execution):
    return bool(execution and can_access_project(user, execution.project))
