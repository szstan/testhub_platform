"""Shared role-aware access for the unified Project model."""

from django.db.models import Q

from .models import Project, ProjectMember


WRITABLE_PROJECT_ROLES = ('owner', 'admin', 'developer', 'tester')


def writable_projects(user, queryset=None):
    """Return only projects in which the authenticated actor can mutate assets."""
    projects = Project.objects.all() if queryset is None else queryset
    if not user or not getattr(user, 'is_authenticated', False):
        return projects.none()
    if user.is_staff or user.is_superuser:
        return projects
    memberships = ProjectMember.objects.filter(
        user_id=user.pk, role__in=WRITABLE_PROJECT_ROLES,
    ).values('project_id')
    return projects.filter(Q(owner_id=user.pk) | Q(pk__in=memberships)).distinct()


def user_can_write_project(user, project):
    """Do not confuse project visibility (including viewers) with write access."""
    if project is None or project.pk is None:
        return False
    return writable_projects(user).filter(pk=project.pk).exists()
