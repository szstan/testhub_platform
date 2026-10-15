"""Authorization helpers for APP automation resources."""
from django.db.models import Q


def is_app_admin(user):
    return bool(
        user is not None
        and getattr(user, 'is_authenticated', False)
        and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))
    )


def accessible_projects(user):
    from .models import AppProject
    if is_app_admin(user):
        return AppProject.objects.all()
    if user is None or not getattr(user, 'is_authenticated', False):
        return AppProject.objects.none()
    return AppProject.objects.filter(Q(owner=user) | Q(members=user)).distinct()


def scope_project(queryset, user, relation='project'):
    if is_app_admin(user):
        return queryset
    if user is None or not getattr(user, 'is_authenticated', False):
        return queryset.none()
    return queryset.filter(
        Q(**{f'{relation}__owner': user})
        | Q(**{f'{relation}__members': user})
    ).distinct()


def scope_test_cases(queryset, user):
    if is_app_admin(user):
        return queryset
    if user is None or not getattr(user, 'is_authenticated', False):
        return queryset.none()
    return queryset.filter(
        Q(project__owner=user) | Q(project__members=user) | Q(created_by=user)
    ).distinct()


def scope_executions(queryset, user):
    if is_app_admin(user):
        return queryset
    if user is None or not getattr(user, 'is_authenticated', False):
        return queryset.none()
    return queryset.filter(
        Q(user=user)
        | Q(test_case__project__owner=user)
        | Q(test_case__project__members=user)
        | Q(test_suite__project__owner=user)
        | Q(test_suite__project__members=user)
    ).distinct()
