"""APP approvals stay bound to their source; oversized native IDs fail at validation."""
import pytest
from rest_framework.test import APIClient

from apps.app_automation.models import AppProject, AppTestCase
from apps.executions.models import ExecutionSourceBinding
from apps.executions.source_contract import SourceBindingError, parse_source_id
from apps.projects.models import Project
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('raw_id', ['9' * 200, '9223372036854775808'])
def test_native_source_id_must_fit_the_database_key_range(raw_id):
    with pytest.raises(SourceBindingError, match='database range'):
        parse_source_id('api', f'api:suite:{raw_id}')


@pytest.fixture
def approved_binding():
    owner = User.objects.create_user(username='approval-scope-owner')
    canonical = Project.objects.create(name='Approval canonical', owner=owner)
    native = AppProject.objects.create(name='Approval native', owner=owner)
    first = AppTestCase.objects.create(project=native, name='approved first case', created_by=owner)
    second = AppTestCase.objects.create(project=native, name='unapproved second case', created_by=owner)
    binding = ExecutionSourceBinding.objects.create(
        project=canonical, source_type='app', source_id=f'app:case:{first.pk}',
        source_project_id=str(native.pk), created_by=owner, metadata={'approved': True},
    )
    client = APIClient()
    client.force_authenticate(owner)
    return owner, canonical, second, binding, client


def test_member_cannot_transplant_existing_app_approval_to_another_case(approved_binding):
    _owner, _canonical, second, binding, client = approved_binding
    old_id = binding.source_id
    response = client.patch(f'/api/executions/source-bindings/{binding.pk}/', {'source_id': f'app:case:{second.pk}'}, format='json')
    assert response.status_code == 403
    binding.refresh_from_db()
    assert binding.source_id == old_id


def test_member_cannot_reparent_an_approved_app_binding(approved_binding):
    owner, canonical, _second, binding, client = approved_binding
    another = Project.objects.create(name='Another canonical', owner=owner)
    response = client.patch(f'/api/executions/source-bindings/{binding.pk}/', {'project': another.pk}, format='json')
    assert response.status_code == 403
    binding.refresh_from_db()
    assert binding.project_id == canonical.pk


def test_reenabling_an_approved_app_binding_requires_a_privileged_actor(approved_binding):
    _owner, _canonical, _second, binding, client = approved_binding
    binding.enabled = False
    binding.save(update_fields=['enabled'])
    response = client.patch(f'/api/executions/source-bindings/{binding.pk}/', {'enabled': True}, format='json')
    assert response.status_code == 403
    binding.refresh_from_db()
    assert binding.enabled is False


def test_privileged_actor_can_explicitly_change_the_approved_source_scope(approved_binding):
    _owner, _canonical, second, binding, client = approved_binding
    admin = User.objects.create_user(username='approval-scope-admin', is_staff=True)
    client.force_authenticate(admin)
    response = client.patch(f'/api/executions/source-bindings/{binding.pk}/', {'source_id': f'app:case:{second.pk}'}, format='json')
    assert response.status_code == 200, response.data
    binding.refresh_from_db()
    assert binding.source_id == f'app:case:{second.pk}'
