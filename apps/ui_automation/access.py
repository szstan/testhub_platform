"""Authorization helpers for UI automation resources."""

from django.conf import settings
from django.db.models import Q


def is_ui_admin(user):
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def selenium_compatibility_enabled(user=None):
    """Return whether legacy Selenium execution is enabled for this deployment."""
    enabled = bool(getattr(settings, 'UI_SELENIUM_COMPATIBILITY', True))
    return enabled or is_ui_admin(user)


def accessible_projects(user):
    from .models import UiProject

    if is_ui_admin(user):
        return UiProject.objects.all()
    if user is None or not getattr(user, "is_authenticated", False):
        return UiProject.objects.none()
    return UiProject.objects.filter(Q(owner=user) | Q(members=user)).distinct()


def project_accessible(user, project):
    if project is None:
        return False
    return is_ui_admin(user) or accessible_projects(user).filter(pk=project.pk).exists()


def scope_project(queryset, user, relation="project"):
    if is_ui_admin(user):
        return queryset
    if user is None or not getattr(user, "is_authenticated", False):
        return queryset.none()
    return queryset.filter(
        Q(**{f"{relation}__owner": user})
        | Q(**{f"{relation}__members": user})
    ).distinct()


def scope_projectless(queryset, user, owner_field="created_by"):
    """Scope projectless records to their creator unless the user is an admin."""
    if is_ui_admin(user):
        return queryset
    if user is None or not getattr(user, "is_authenticated", False):
        return queryset.none()
    return queryset.filter(**{owner_field: user})


def same_project(left, right):
    """Return true when two project-owned resources belong to one project."""
    return bool(left is not None and right is not None and left.project_id == right.project_id)
