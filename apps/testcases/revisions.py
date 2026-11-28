from django.db import transaction

from apps.executions.models import TestCaseRevision
from .models import TestCase


def test_case_snapshot(test_case):
    return {
        'title': test_case.title,
        'external_ref': test_case.external_ref,
        'description': test_case.description,
        'preconditions': test_case.preconditions,
        'steps': test_case.steps,
        'expected_result': test_case.expected_result,
        'priority': test_case.priority,
        'status': test_case.status,
        'test_type': test_case.test_type,
        'tags': test_case.tags,
        'project_id': test_case.project_id,
        'version_ids': list(
            test_case.versions.filter(projects__id=test_case.project_id)
            .order_by('id').values_list('id', flat=True)
        ),
    }


@transaction.atomic
def record_test_case_revision(test_case, user):
    test_case = TestCase.objects.select_for_update().get(pk=test_case.pk)
    latest = (
        TestCaseRevision.objects.filter(test_case=test_case)
        .order_by('-revision').values_list('revision', flat=True).first()
        or 0
    )
    return TestCaseRevision.objects.create(
        test_case=test_case,
        revision=latest + 1,
        snapshot=test_case_snapshot(test_case),
        changed_by=user,
    )
