from collections import Counter
import re

from django.db import transaction
from django.utils import timezone

from apps.projects.models import Project
from apps.requirement_analysis.ai_contracts import AIContractError, validate_executable_cases
from apps.requirement_analysis.permissions import user_can_write_project
from apps.testcases.models import TestCase


MAX_ADOPTION_BATCH_SIZE = 200
LEGACY_PRIORITY_MAP = {
    'P0': 'critical',
    'P1': 'high',
    'P2': 'medium',
    'P3': 'low',
}


class AdoptionError(ValueError):
    def __init__(self, message, status_code=400, extra=None):
        super().__init__(message)
        self.status_code = status_code
        self.extra = extra or {}


def _legacy_adopted_case_ids(task, canonical_cases):
    if not task.project_id or not canonical_cases:
        return []

    titles = {case['title'] for case in canonical_cases}
    saved_cases = TestCase.objects.filter(
        project_id=task.project_id,
        author_id=task.created_by_id,
        created_at__gte=task.created_at,
        title__in=titles,
    ).values_list('title', 'description', 'preconditions', 'steps', 'expected_result', 'priority')
    remaining_matches = Counter(saved_cases)
    adopted_ids = []

    for case in canonical_cases:
        signature = (
            case['title'],
            case['title'],
            case['preconditions'],
            '\n'.join(case['steps']),
            case['expected_result'],
            LEGACY_PRIORITY_MAP.get(case['priority'], 'medium'),
        )
        if remaining_matches[signature]:
            adopted_ids.append(case['case_id'])
            remaining_matches[signature] -= 1
    return adopted_ids


def previous_adopted_case_ids(task, canonical_cases):
    stored_ids = task.adopted_case_ids
    if not isinstance(stored_ids, list):
        stored_ids = []
    adopted_ids = list(dict.fromkeys(
        case_id.strip() for case_id in stored_ids if isinstance(case_id, str) and case_id.strip()
    ))
    if task.is_saved_to_records and not adopted_ids:
        adopted_ids = _legacy_adopted_case_ids(task, canonical_cases)
    return adopted_ids


def select_case_ids(
    task,
    canonical_cases,
    requested_ids=None,
    *,
    all_remaining=False,
    limit=MAX_ADOPTION_BATCH_SIZE,
):
    canonical_ids = [case['case_id'] for case in canonical_cases]
    canonical_id_set = set(canonical_ids)

    if all_remaining and requested_ids is None:
        adopted = set(previous_adopted_case_ids(task, canonical_cases))
        selected_ids = [case_id for case_id in canonical_ids if case_id not in adopted]
        return selected_ids if limit is None else selected_ids[:limit]

    if not isinstance(requested_ids, list) or not requested_ids:
        raise AdoptionError('case_ids 必须是非空数组')
    if len(requested_ids) > MAX_ADOPTION_BATCH_SIZE:
        raise AdoptionError(f'一次最多采纳 {MAX_ADOPTION_BATCH_SIZE} 条测试用例')
    if any(not isinstance(case_id, str) or not case_id.strip() for case_id in requested_ids):
        raise AdoptionError('每个 case_id 都必须是非空字符串')

    case_ids = [case_id.strip() for case_id in requested_ids]
    if len(set(case_ids)) != len(case_ids):
        raise AdoptionError('同一测试用例不能重复采纳')
    unknown_ids = [case_id for case_id in case_ids if case_id not in canonical_id_set]
    if unknown_ids:
        raise AdoptionError(
            '只能采纳任务最终输出中的测试用例',
            extra={'unknown_case_ids': unknown_ids},
        )
    return case_ids


def resolve_project(task, user, requested_project_id=None):
    if task.project_id:
        project = task.project
        if not user_can_write_project(user, project):
            raise AdoptionError('无权使用该项目', status_code=403)
        return project
    if requested_project_id in (None, ''):
        raise AdoptionError('任务未绑定项目，请提供可访问的 project_id')
    if (
        isinstance(requested_project_id, bool)
        or not isinstance(requested_project_id, (int, str))
        or not re.fullmatch(r'[1-9]\d*', str(requested_project_id).strip())
    ):
        raise AdoptionError('project_id 无效')

    project = Project.objects.filter(pk=int(requested_project_id)).first()
    if project is None or not user_can_write_project(user, project):
        raise AdoptionError('无权使用该项目', status_code=403)
    return project


@transaction.atomic
def adopt_cases(task, canonical_cases, case_ids, user, requested_project_id=None):
    canonical_by_id = {case['case_id']: case for case in canonical_cases}
    selected_cases = [canonical_by_id[case_id] for case_id in case_ids]
    try:
        validate_executable_cases(selected_cases)
    except AIContractError as exc:
        raise AdoptionError(str(exc)) from exc

    adopted_ids = previous_adopted_case_ids(task, canonical_cases)
    adopted_set = set(adopted_ids)
    cases_to_adopt = [case for case in selected_cases if case['case_id'] not in adopted_set]
    project = (
        resolve_project(task, user, requested_project_id)
        if task.project_id or cases_to_adopt
        else None
    )

    with transaction.atomic():
        for case in cases_to_adopt:
            TestCase.objects.create(
                project=project,
                author=task.created_by,
                title=case['title'],
                description=case['title'],
                preconditions=case['preconditions'],
                steps='\n'.join(case['steps']),
                expected_result=case['expected_result'],
                priority={
                    'P0': 'critical',
                    'P1': 'high',
                    'P2': 'medium',
                    'P3': 'low',
                }.get(case['priority'], 'medium'),
                test_type='functional',
                status='draft',
            )

        updated_adopted_ids = list(dict.fromkeys([
            *adopted_ids,
            *[case['case_id'] for case in cases_to_adopt],
        ]))
        state_changed = (
            updated_adopted_ids != (task.adopted_case_ids or [])
            or task.is_saved_to_records != bool(updated_adopted_ids)
        )
        task.adopted_case_ids = updated_adopted_ids
        task.is_saved_to_records = bool(updated_adopted_ids)
        if cases_to_adopt and task.saved_at is None:
            task.saved_at = timezone.now()
            state_changed = True
        if state_changed:
            task.save(update_fields=['adopted_case_ids', 'is_saved_to_records', 'saved_at'])

    adopted_case_id_set = set(updated_adopted_ids)
    remaining_count = sum(
        case['case_id'] not in adopted_case_id_set
        for case in canonical_cases
    )
    return {
        'adopted_count': len(cases_to_adopt),
        'already_adopted': not cases_to_adopt,
        'adopted_case_ids': updated_adopted_ids,
        'remaining_count': remaining_count,
        'project_name': project.name if project else None,
    }
