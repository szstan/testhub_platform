import pytest
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.requirement_analysis.ai_contracts import parse_generated_cases
from apps.requirement_analysis.models import TestCaseGenerationTask as GenerationTask
from apps.testcases.models import TestCase as SavedTestCase
from apps.users.models import User
from apps.requirement_analysis.views import TestCaseGenerationTaskViewSet as GenerationTaskViewSet


pytestmark = pytest.mark.django_db


CURRENT_MARKDOWN = """## AI-001: Login succeeds
- Priority: P1

### Preconditions
A registered user exists.

### Steps
1. Enter valid credentials.
2. Submit the login form.

### Expected result
The user is redirected to the dashboard.

## AI-002: Login rejects invalid credentials
- Priority: P2

### Preconditions
A registered user exists.

### Steps
1. Enter an invalid password.
2. Submit the login form.

### Expected result
An error is shown and the session remains unauthenticated.
"""


def make_user(username):
    return User.objects.create_user(username=username, password='StrongPass123')


def make_task(user, project=None, content=CURRENT_MARKDOWN):
    return GenerationTask.objects.create(
        task_id=f'TASK_{user.username.upper()}',
        title='Generated cases',
        requirement_text='Login behavior',
        status='completed',
        final_test_cases=content,
        project=project,
        created_by=user,
    )


def adoption_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def adoption_url(task):
    return f'/api/requirement-analysis/testcase-generation/{task.task_id}/batch-adopt-selected/'


def batch_adoption_url(task):
    return f'/api/requirement-analysis/testcase-generation/{task.task_id}/batch_adopt/'


def save_to_records_url(task):
    return f'/api/requirement-analysis/testcase-generation/{task.task_id}/save_to_records/'


def test_current_markdown_is_exposed_and_selected_adoption_uses_case_ids():
    user = make_user('adoption-contract')
    project = Project.objects.create(name='Adoption project', owner=user)
    task = make_task(user, project)
    client = adoption_client(user)

    old_parser_cases = GenerationTaskViewSet()._parse_text_format(CURRENT_MARKDOWN)
    assert old_parser_cases
    assert not any(case.get('scenario') == 'Login succeeds' for case in old_parser_cases)

    detail = client.get(f'/api/requirement-analysis/testcase-generation/{task.task_id}/')
    assert detail.status_code == 200
    assert [case['case_id'] for case in detail.json()['canonical_cases']] == ['AI-001', 'AI-002']
    assert detail.json()['canonical_cases'][0] == {
        'case_id': 'AI-001',
        'title': 'Login succeeds',
        'priority': 'P1',
        'preconditions': 'A registered user exists.',
        'steps': ['Enter valid credentials.', 'Submit the login form.'],
        'expected_result': 'The user is redirected to the dashboard.',
    }

    old_title_request = client.post(
        adoption_url(task), {'test_cases': [{'title': 'Login succeeds'}]}, format='json'
    )
    assert old_title_request.status_code == 400

    response = client.post(adoption_url(task), {'case_ids': ['AI-001']}, format='json')
    assert response.status_code == 200
    assert response.json()['adopted_count'] == 1
    saved_case = SavedTestCase.objects.get(project=project)
    assert saved_case.title == 'Login succeeds'
    assert saved_case.preconditions == 'A registered user exists.'
    assert saved_case.steps == 'Enter valid credentials.\nSubmit the login form.'
    assert saved_case.expected_result == 'The user is redirected to the dashboard.'
    assert saved_case.priority == 'high'


def test_legacy_table_and_text_outputs_are_normalized_with_stable_ids():
    table = """| 用例ID | 测试目标 | 前置条件 | 测试步骤 | 预期结果 | 优先级 |
|---|---|---|---|---|---|
| TC-001 | Search returns results | Index is ready | 1. Enter query<br>2. Search | Results are shown | P1 |"""
    text = """测试用例 TC-LEGACY-2: Search rejects empty query
前置条件: Search page is open.
测试步骤:
1. Leave the query empty.
2. Submit the search form.
预期结果: A validation message is shown.
优先级: 高"""

    table_cases = parse_generated_cases(table)
    text_cases = parse_generated_cases(text)

    assert table_cases == [{
        'case_id': 'TC-001',
        'title': 'Search returns results',
        'priority': 'P1',
        'preconditions': 'Index is ready',
        'steps': ['Enter query', 'Search'],
        'expected_result': 'Results are shown',
    }]
    assert text_cases == [{
        'case_id': 'TC-LEGACY-2',
        'title': 'Search rejects empty query',
        'priority': 'P1',
        'preconditions': 'Search page is open.',
        'steps': ['Leave the query empty.', 'Submit the search form.'],
        'expected_result': 'A validation message is shown.',
    }]


def test_edit_then_adopt_accepts_the_frontend_markdown_table_format():
    user = make_user('adoption-edit-then-adopt')
    project = Project.objects.create(name='Edited adoption project', owner=user)
    task = make_task(user, project)
    client = adoption_client(user)
    edited_markdown = """用例ID | 测试目标 | 前置条件 | 测试步骤 | 预期结果 | 优先级
--- | --- | --- | --- | --- | ---
AI-001 | Edited login succeeds | A registered user exists. | Enter valid credentials.<br>Submit the login form. | The dashboard appears. | P1
"""

    updated = client.post(
        f'/api/requirement-analysis/testcase-generation/{task.task_id}/update-test-cases/',
        {'final_test_cases': edited_markdown},
        format='json',
    )
    assert updated.status_code == 200
    detail = client.get(f'/api/requirement-analysis/testcase-generation/{task.task_id}/')
    assert [(case['case_id'], case['title']) for case in detail.json()['canonical_cases']] == [
        ('AI-001', 'Edited login succeeds'),
    ]

    adopted = client.post(adoption_url(task), {'case_ids': ['AI-001']}, format='json')

    assert adopted.status_code == 200
    assert SavedTestCase.objects.get(project=project).title == 'Edited login succeeds'


def test_unknown_and_duplicate_case_ids_are_rejected_without_creating_cases():
    user = make_user('adoption-invalid-ids')
    project = Project.objects.create(name='Adoption project', owner=user)
    task = make_task(user, project)
    client = adoption_client(user)

    unknown = client.post(adoption_url(task), {'case_ids': ['NOT-A-CASE']}, format='json')
    duplicate = client.post(adoption_url(task), {'case_ids': ['AI-001', 'AI-001']}, format='json')

    assert unknown.status_code == 400
    assert duplicate.status_code == 400
    assert SavedTestCase.objects.filter(project=project).count() == 0
    task.refresh_from_db()
    assert task.adopted_case_ids == []


def test_unbound_task_requires_an_accessible_explicit_project():
    user = make_user('adoption-unbound')
    outsider = make_user('adoption-unbound-outsider')
    permitted_project = Project.objects.create(name='My explicit project', owner=user)
    foreign_project = Project.objects.create(name='Foreign project', owner=outsider)
    task = make_task(user)
    client = adoption_client(user)

    missing = client.post(adoption_url(task), {'case_ids': ['AI-001']}, format='json')
    unauthorized = client.post(
        adoption_url(task), {'case_ids': ['AI-001'], 'project_id': foreign_project.pk}, format='json'
    )
    permitted = client.post(
        adoption_url(task), {'case_ids': ['AI-001'], 'project_id': permitted_project.pk}, format='json'
    )

    assert missing.status_code == 400
    assert unauthorized.status_code == 403
    assert permitted.status_code == 200
    assert SavedTestCase.objects.filter(project=permitted_project).count() == 1


def test_bound_project_wins_and_partial_adoption_retries_are_idempotent():
    user = make_user('adoption-partial')
    bound_project = Project.objects.create(name='Bound project', owner=user)
    other_project = Project.objects.create(name='Other project', owner=user)
    task = make_task(user, bound_project)
    client = adoption_client(user)

    first = client.post(
        adoption_url(task), {'case_ids': ['AI-001'], 'project_id': other_project.pk}, format='json'
    )
    retry = client.post(adoption_url(task), {'case_ids': ['AI-001']}, format='json')
    second = client.post(adoption_url(task), {'case_ids': ['AI-002']}, format='json')

    assert first.status_code == retry.status_code == second.status_code == 200
    assert first.json()['adopted_count'] == 1
    assert retry.json()['adopted_count'] == 0
    assert retry.json()['already_adopted'] is True
    assert second.json()['adopted_count'] == 1
    assert SavedTestCase.objects.filter(project=bound_project).count() == 2
    assert SavedTestCase.objects.filter(project=other_project).count() == 0
    task.refresh_from_db()
    assert task.adopted_case_ids == ['AI-001', 'AI-002']
    assert task.is_saved_to_records is True


def test_adoption_create_failure_rolls_back_cases_and_task_state(monkeypatch):
    user = make_user('adoption-rollback')
    project = Project.objects.create(name='Rollback project', owner=user)
    task = make_task(user, project)
    client = adoption_client(user)

    def fail_create(**kwargs):
        raise RuntimeError('simulated create failure')

    monkeypatch.setattr(SavedTestCase.objects, 'create', fail_create)
    response = client.post(adoption_url(task), {'case_ids': ['AI-001']}, format='json')

    assert response.status_code == 500
    assert SavedTestCase.objects.filter(project=project).count() == 0
    task.refresh_from_db()
    assert task.adopted_case_ids == []
    assert task.is_saved_to_records is False
    assert task.saved_at is None


def test_batch_adoption_requires_an_explicit_accessible_project_and_adopts_all_cases():
    user = make_user('adoption-batch-unbound')
    outsider = make_user('adoption-batch-outsider')
    permitted_project = Project.objects.create(name='Batch project', owner=user)
    foreign_project = Project.objects.create(name='Foreign batch project', owner=outsider)
    task = make_task(user)
    client = adoption_client(user)

    missing = client.post(batch_adoption_url(task), {}, format='json')
    unauthorized = client.post(
        batch_adoption_url(task), {'project_id': foreign_project.pk}, format='json'
    )
    adopted = client.post(
        batch_adoption_url(task), {'project_id': permitted_project.pk}, format='json'
    )

    assert missing.status_code == 400
    assert unauthorized.status_code == 403
    assert adopted.status_code == 200
    assert adopted.json()['adopted_count'] == 2
    assert adopted.json()['remaining_count'] == 0
    assert SavedTestCase.objects.filter(project=permitted_project).count() == 2
    task.refresh_from_db()
    assert task.adopted_case_ids == ['AI-001', 'AI-002']


def test_bound_project_access_is_rechecked_when_task_creator_loses_membership():
    user = make_user('adoption-revoked-member')
    owner = make_user('adoption-project-owner')
    project = Project.objects.create(name='Revoked project', owner=owner)
    project.members.add(user)
    task = make_task(user, project)
    project.members.remove(user)
    client = adoption_client(user)

    response = client.post(adoption_url(task), {'case_ids': ['AI-001']}, format='json')

    assert response.status_code == 403
    assert SavedTestCase.objects.filter(project=project).count() == 0
    task.refresh_from_db()
    assert task.adopted_case_ids == []


def test_legacy_saved_flag_reconciles_only_existing_cases_before_retrying_adoption():
    user = make_user('adoption-legacy-partial')
    project = Project.objects.create(name='Legacy adoption project', owner=user)
    rows = [
        f'AI-{index:03d} | Legacy case {index:03d} | A user exists. | Open the page. | Page opens. | P2'
        for index in range(1, 202)
    ]
    content = '\n'.join([
        '用例ID | 测试目标 | 前置条件 | 测试步骤 | 预期结果 | 优先级',
        '--- | --- | --- | --- | --- | ---',
        *rows,
        '',
    ])
    task = make_task(user, project, content)
    task.is_saved_to_records = True
    task.save(update_fields=['is_saved_to_records'])
    SavedTestCase.objects.bulk_create([
        SavedTestCase(
            project=project,
            author=user,
            title=f'Legacy case {index:03d}',
            description=f'Legacy case {index:03d}',
            preconditions='A user exists.',
            steps='Open the page.',
            expected_result='Page opens.',
            priority='medium',
            test_type='functional',
            status='draft',
        )
        for index in range(1, 201)
    ])
    client = adoption_client(user)

    missing_case = client.post(adoption_url(task), {'case_ids': ['AI-201']}, format='json')
    adopted_retry = client.post(adoption_url(task), {'case_ids': ['AI-001']}, format='json')

    assert missing_case.status_code == adopted_retry.status_code == 200
    assert missing_case.json()['adopted_count'] == 1
    assert missing_case.json()['remaining_count'] == 0
    assert adopted_retry.json()['adopted_count'] == 0
    assert SavedTestCase.objects.filter(project=project).count() == 201
    task.refresh_from_db()
    assert len(task.adopted_case_ids) == 201


def test_save_to_records_uses_case_ids_and_adopts_only_remaining_cases():
    user = make_user('adoption-save-records')
    project = Project.objects.create(name='Records project', owner=user)
    task = make_task(user, project)
    client = adoption_client(user)

    selected = client.post(adoption_url(task), {'case_ids': ['AI-001']}, format='json')
    saved = client.post(save_to_records_url(task), {}, format='json')
    retry = client.post(save_to_records_url(task), {}, format='json')

    assert selected.status_code == saved.status_code == retry.status_code == 200
    assert saved.json()['imported_count'] == 1
    assert saved.json()['remaining_count'] == 0
    assert retry.json()['imported_count'] == 0
    assert retry.json()['already_saved'] is True
    assert SavedTestCase.objects.filter(project=project).count() == 2
    task.refresh_from_db()
    assert task.adopted_case_ids == ['AI-001', 'AI-002']
