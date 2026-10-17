"""Authorization helpers for monitor resources."""
from django.db.models import Q


def is_monitor_admin(user):
    return bool(
        user is not None
        and getattr(user, 'is_authenticated', False)
        and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))
    )


def scope_owned(queryset, user, relation=''):
    if is_monitor_admin(user):
        return queryset
    if user is None or not getattr(user, 'is_authenticated', False):
        return queryset.none()
    field = f'{relation}created_by' if relation else 'created_by'
    return queryset.filter(**{field: user})


def scope_target_related(queryset, user, relation='target__'):
    return scope_owned(queryset, user, relation)
