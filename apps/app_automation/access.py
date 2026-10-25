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


def project_accessible(user, project):
    if project is None:
        return False
    return is_app_admin(user) or accessible_projects(user).filter(pk=project.pk).exists()


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


def device_action_allowed(user, device):
    """A regular user may inspect/use free devices or their own lease only."""
    return bool(
        device is not None
        and (
            is_app_admin(user)
            or device.locked_by_id is None
            or device.locked_by_id == getattr(user, 'id', None)
        )
    )


def package_action_allowed(user, package, project):
    """Packages are global records; a named owner must belong to the task project."""
    if package is None or is_app_admin(user) or package.created_by_id is None:
        return True
    if project is None:
        return package.created_by_id == getattr(user, 'id', None)
    return bool(
        package.created_by_id == project.owner_id
        or project.members.filter(pk=package.created_by_id).exists()
    )


def scheduled_task_relation_errors(*, user, project, device=None, app_package=None,
                                   test_suite=None, test_case=None, task_type=None):
    """Validate all APP scheduled-task foreign keys against one project."""
    errors = {}
    if project is None:
        errors['project'] = '请选择有效的 APP 项目'
    elif user is not None and getattr(user, 'is_authenticated', False) and not project_accessible(user, project):
        errors['project'] = '无权使用该 APP 项目'

    if device is not None and not device_action_allowed(user, device):
        errors['device'] = '无权使用其他用户锁定的设备'

    if app_package is not None and not package_action_allowed(user, app_package, project):
        errors['app_package'] = '应用包名不属于该项目可用范围'

    if test_suite is not None:
        if project is None or test_suite.project_id != project.id:
            errors['test_suite'] = '测试套件必须属于同一项目'
        else:
            invalid_cases = test_suite.suite_cases.exclude(test_case__project_id=project.id).exists()
            if invalid_cases:
                errors['test_suite'] = '测试套件包含其他项目的测试用例'

    if test_case is not None and (project is None or test_case.project_id != project.id):
        errors['test_case'] = '测试用例必须属于同一项目'

    if test_case is not None and app_package is not None and test_case.app_package_id not in (None, app_package.id):
        errors['app_package'] = '应用包名必须与测试用例一致'

    if test_suite is not None and app_package is not None:
        package_ids = set(
            test_suite.suite_cases.exclude(test_case__app_package_id__isnull=True)
            .values_list('test_case__app_package_id', flat=True)
        )
        if package_ids and package_ids != {app_package.id}:
            errors['app_package'] = '应用包名必须与测试套件中的用例一致'

    if task_type == 'TEST_SUITE' and test_suite is None:
        errors['test_suite'] = '请选择测试套件'
    if task_type == 'TEST_CASE' and test_case is None:
        errors['test_case'] = '请选择测试用例'
    return errors
