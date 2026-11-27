from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.app_automation.constants import ElementType, ExecutionEngine
from apps.app_automation.models import (
    AppElement,
    AppPackage,
    AppProject,
    AppProjectComponent,
    AppTestCase,
    AppTestExecution,
    AppTestSuite,
    AppTestSuiteCase,
    AppScheduledTask,
    AppDevice,
)
from apps.app_automation.scene_execution import (
    ScenePreparationError,
    check_appium_prerequisites,
    prepare_scene_execution,
)
from apps.app_automation.serializers import AppElementSerializer, AppTestCaseSerializer
from apps.users.models import User


@pytest.fixture
def appium_scene(db):
    owner = User.objects.create_user(username='appium-contract-owner')
    project = AppProject.objects.create(
        name='appium contract project', owner=owner, appium_enabled=True,
    )
    package = AppPackage.objects.create(
        name='Contract app', package_name='com.example.contract', created_by=owner,
    )
    element = AppElement.objects.create(
        project=project,
        name='contract submit',
        element_type=ElementType.NATIVE,
        config={
            'package_name': package.package_name,
            'strategy': 'id',
            'value': 'com.example.contract:id/submit',
            'container': {'strategy': 'accessibility_id', 'value': 'checkout'},
        },
        created_by=owner,
    )
    case = AppTestCase.objects.create(
        project=project,
        app_package=package,
        name='appium contract case',
        created_by=owner,
        ui_flow=[{'type': 'click', 'element_id': element.pk}],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=owner)
    return SimpleNamespace(
        owner=owner, project=project, package=package, element=element,
        case=case, execution=execution,
    )


@pytest.mark.django_db
def test_appium_preparation_accepts_visual_steps_and_strips_client_locator_maps(appium_scene):
    scene = appium_scene
    visual = AppElement.objects.create(
        project=scene.project, name='contract visual point', element_type=ElementType.POS,
        config={'x': 0, 'y': 20}, created_by=scene.owner,
    )
    scene.case.ui_flow = [
        {'type': 'click', 'config': {'element_id': visual.pk, 'resolved_appium_elements': {'poisoned': True}}},
        {'type': 'click', 'selector_type': 'relative', 'selector': '0.2,0.4',
         'resolved_appium_elements': {'poisoned': True}},
        {'type': 'click', 'element_id': scene.element.pk},
    ]
    prepared, _ = prepare_scene_execution(scene.case, scene.execution)
    assert 'resolved_appium_elements' not in prepared[0]['config']
    assert 'resolved_appium_elements' not in prepared[1]
    assert prepared[2]['resolved_appium_elements']['element_id']['element_id'] == scene.element.pk


@pytest.mark.django_db
def test_project_flag_defaults_new_cases_to_appium_and_execution_snapshots_engine(appium_scene):
    scene = appium_scene
    assert scene.case.engine == ExecutionEngine.APPIUM
    assert scene.execution.engine == ExecutionEngine.APPIUM

    scene.case.engine = ExecutionEngine.LEGACY
    scene.case.save(update_fields=['engine'])
    scene.execution.refresh_from_db()
    assert scene.execution.engine == ExecutionEngine.APPIUM

    disabled_project = AppProject.objects.create(name='legacy default', owner=scene.owner)
    legacy_case = AppTestCase.objects.create(name='legacy case', project=disabled_project)
    assert disabled_project.appium_enabled is False
    assert legacy_case.engine == ExecutionEngine.LEGACY
    assert AppTestExecution.objects.create(test_case=legacy_case).engine == ExecutionEngine.LEGACY


@pytest.mark.django_db
def test_case_serializer_uses_project_default_and_rejects_appium_when_disabled(appium_scene):
    scene = appium_scene
    request = SimpleNamespace(user=scene.owner)
    serializer = AppTestCaseSerializer(
        data={
            'name': 'serializer default case',
            'project': scene.project.pk,
            'app_package': scene.package.pk,
            'ui_flow': [],
        },
        context={'request': request},
    )
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data['engine'] == ExecutionEngine.APPIUM

    disabled_project = AppProject.objects.create(name='disabled serializer project', owner=scene.owner)
    rejected = AppTestCaseSerializer(
        data={
            'name': 'rejected appium case',
            'project': disabled_project.pk,
            'app_package': scene.package.pk,
            'engine': ExecutionEngine.APPIUM,
        },
        context={'request': request},
    )
    assert not rejected.is_valid()
    assert 'engine' in rejected.errors


@pytest.mark.django_db
def test_native_element_serializer_enforces_exact_project_scoped_config(appium_scene):
    scene = appium_scene
    request = SimpleNamespace(user=scene.owner)
    config = {
        'package_name': scene.package.package_name,
        'strategy': 'accessibility_id',
        'value': 'continue',
    }
    valid = AppElementSerializer(
        data={
            'name': 'new native element',
            'project': scene.project.pk,
            'element_type': ElementType.NATIVE,
            'config': config,
        },
        context={'request': request},
    )
    assert valid.is_valid(), valid.errors

    invalid = AppElementSerializer(
        data={
            'name': 'invalid native element',
            'project': scene.project.pk,
            'element_type': ElementType.NATIVE,
            'config': {**config, 'xpath': '//button'},
        },
        context={'request': request},
    )
    assert not invalid.is_valid()
    assert 'config' in invalid.errors


@pytest.mark.django_db
def test_preparation_pins_native_definitions_without_mutating_saved_flow(appium_scene):
    scene = appium_scene
    original = [{'type': 'click', 'element_id': scene.element.pk}]
    scene.case.ui_flow = original
    scene.case.save(update_fields=['ui_flow'])

    report = check_appium_prerequisites(scene.case, user=scene.owner)
    assert report['ready'] is True
    assert report['scope'] == 'case_configuration'
    assert {check['name'] for check in report['checks']} == {
        'project_access', 'project_appium_enabled', 'android_package',
        'supported_actions', 'native_elements',
    }

    flow, _variables = prepare_scene_execution(scene.case, scene.execution)
    assert flow[0]['element_id'] == scene.element.pk
    assert flow[0]['resolved_appium_elements'] == {
        'element_id': {
            'element_id': scene.element.pk,
            'package_name': scene.package.package_name,
            'strategy': 'id',
            'value': 'com.example.contract:id/submit',
            'container': {'strategy': 'accessibility_id', 'value': 'checkout'},
        },
    }
    assert scene.case.ui_flow == original
    assert 'resolved_appium_elements' not in scene.case.ui_flow[0]


@pytest.mark.django_db
def test_component_expansion_resolves_native_ids_and_rejects_cross_project_elements(appium_scene):
    scene = appium_scene
    component = AppProjectComponent.objects.create(
        project=scene.project,
        name='native submit',
        type='native_submit',
        version='1',
        schema={},
        default_config={},
        steps=[{'type': 'click', 'config': {'element_id': scene.element.pk}}],
    )
    scene.case.ui_flow = [{
        'kind': 'custom', 'type': component.type, 'version': component.version, 'params': {},
    }]
    scene.case.save(update_fields=['ui_flow'])
    flow, _variables = prepare_scene_execution(scene.case, scene.execution)
    assert flow[0]['resolved_appium_elements']['config.element_id']['element_id'] == scene.element.pk

    other_project = AppProject.objects.create(name='other appium project', owner=scene.owner, appium_enabled=True)
    other_element = AppElement.objects.create(
        project=other_project,
        name='foreign native element',
        element_type=ElementType.NATIVE,
        config={
            'package_name': scene.package.package_name,
            'strategy': 'id',
            'value': 'com.example.contract:id/foreign',
        },
    )
    scene.case.ui_flow = [{'type': 'click', 'element_id': other_element.pk}]
    scene.case.save(update_fields=['ui_flow'])
    report = check_appium_prerequisites(scene.case, user=scene.owner)
    assert report['ready'] is False
    assert any(issue['code'] == 'native_element_unavailable' for issue in report['issues'])
    with pytest.raises(ScenePreparationError, match='不属于当前项目/应用'):
        prepare_scene_execution(scene.case, scene.execution)


@pytest.mark.django_db
def test_prepared_appium_native_click_rejects_late_match(appium_scene, monkeypatch, tmp_path):
    from apps.app_automation import appium_locator
    from apps.app_automation.runners.ui_flow_runner import UiFlowRunner

    scene = appium_scene
    scene.case.ui_flow = [{'type': 'click', 'element_id': scene.element.pk, 'timeout': 0.2}]
    scene.case.save(update_fields=['ui_flow'])
    prepared, _variables = prepare_scene_execution(scene.case, scene.execution)

    now = [0.0]
    monkeypatch.setattr(appium_locator.time, 'monotonic', lambda: now[0])
    element = Mock()
    element.is_displayed.return_value = True
    element.is_enabled.return_value = True
    driver = SimpleNamespace(
        current_package=scene.package.package_name,
        find_elements=Mock(side_effect=lambda *_args: now.__setitem__(0, 0.25) or [element]),
    )
    runner = UiFlowRunner(image_base_dir=str(tmp_path), appium_driver=driver)
    runner._attach_allure = Mock()

    with pytest.raises(appium_locator.AppiumLocatorFailure) as failure:
        runner.run(prepared, runtime={'stop_on_error': True})

    assert failure.value.code == 'element_not_found'
    element.is_displayed.assert_not_called()
    element.click.assert_not_called()


@pytest.mark.django_db
def test_appium_preflight_accepts_runner_visual_and_coordinate_actions(appium_scene):
    scene = appium_scene
    scene.case.ui_flow = [
        {'type': 'swipe', 'direction': 'left'},
        {
            'type': 'drag', 'start_selector_type': 'pos', 'start_selector': '1,2',
            'end_selector_type': 'pos', 'end_selector': '3,4',
        },
        {'type': 'screenshot'},
        {'type': 'image_exists_click', 'selector': 'submit.png'},
        {
            'type': 'image_exists_click_chain', 'selector': 'continue.png',
            'fallback_selector': 'retry.png',
        },
        {'type': 'swipe_to', 'target_selector': 'next.png'},
        {'type': 'foreach_assert', 'click_selector_type': 'pos', 'click_selector': '5,6'},
    ]
    scene.case.save(update_fields=['ui_flow'])

    report = check_appium_prerequisites(scene.case, user=scene.owner)
    prepared, _variables = prepare_scene_execution(scene.case, scene.execution)

    assert report['ready'] is True, report['issues']
    assert [step['type'] for step in prepared] == [
        'swipe', 'drag', 'screenshot', 'image_exists_click', 'image_exists_click_chain',
        'swipe_to', 'foreach_assert',
    ]


@pytest.mark.django_db
def test_drag_selected_from_the_element_library_is_runnable_and_blocks_archive(appium_scene, tmp_path, monkeypatch):
    from apps.app_automation.runners import ui_flow_runner as flow_runner

    scene = appium_scene
    points = [AppElement.objects.create(
        project=scene.project, name=f'Drag point {index}', element_type=ElementType.POS,
        config={'x': index, 'y': index + 1}, created_by=scene.owner,
    ) for index in (1, 3)]
    scene.case.ui_flow = [{'type': 'drag', 'config': {
        'start_element_id': points[0].pk, 'start_selector_type': 'pos', 'start_selector': '1,2',
        'end_element_id': points[1].pk, 'end_selector_type': 'pos', 'end_selector': '3,4',
    }}]
    scene.case.save(update_fields=['ui_flow'])
    prepared, _ = prepare_scene_execution(scene.case, scene.execution)
    fake_swipe = Mock()
    monkeypatch.setattr(flow_runner, 'G', SimpleNamespace(DEVICE=object()))
    monkeypatch.setattr(flow_runner, 'swipe', fake_swipe)
    runner = flow_runner.UiFlowRunner(image_base_dir=str(tmp_path), appium_driver=object())
    assert runner.run(prepared, runtime={'stop_on_error': True})['passed'] == 1
    fake_swipe.assert_called_once_with((1, 2), (3, 4), duration=0.8)

    client = APIClient()
    client.force_authenticate(scene.owner)
    assert client.delete(f'/api/app-automation/elements/{points[0].pk}/').status_code == 409


@pytest.mark.django_db
def test_drag_start_category_override_and_legacy_end_use_the_saved_template_paths(appium_scene, tmp_path, monkeypatch):
    from apps.app_automation.runners import ui_flow_runner as flow_runner

    scene = appium_scene
    scene.case.ui_flow = [{'type': 'drag', 'config': {
        'start_selector_type': 'image', 'start_selector': 'move.png', 'start_image_scope': 'new_screen',
        'end_selector_type': 'image', 'end_selector': 'drop.png', 'image_scope': 'legacy',
    }}]
    prepared, _ = prepare_scene_execution(scene.case, scene.execution)
    expected_paths = [tmp_path / 'new_screen' / 'move.png', tmp_path / 'legacy' / 'drop.png']
    for path in expected_paths:
        path.parent.mkdir()
        path.touch()
    template_factory = Mock(side_effect=lambda path, **_kwargs: SimpleNamespace(path=path, match_in=Mock()))
    monkeypatch.setattr(flow_runner, 'Template', template_factory)
    monkeypatch.setattr(flow_runner, 'G', SimpleNamespace(DEVICE=object()))
    fake_swipe = Mock()
    monkeypatch.setattr(flow_runner, 'swipe', fake_swipe)
    runner = flow_runner.UiFlowRunner(image_base_dir=str(tmp_path), appium_driver=object())
    runner._image_lookup_once = Mock(side_effect=[(1, 2), (3, 4)])

    assert runner.run(prepared, runtime={'stop_on_error': True})['passed'] == 1
    assert [call.args[0] for call in template_factory.call_args_list] == [str(path) for path in expected_paths]
    fake_swipe.assert_called_once_with((1, 2), (3, 4), duration=0.8)


@pytest.mark.django_db
@pytest.mark.parametrize('step', [
    {'type': 'swipe'},
    {'type': 'swipe', 'direction': []},
    {'type': 'drag', 'start_selector': '1,2'},
    {'type': 'drag', 'start_selector': '1,2', 'end_selector': '3,4', 'start_selector_type': []},
    {'type': 'swipe_to'},
    {'type': 'image_exists_click'},
    {'type': 'image_exists_click_chain', 'selector': 'main.png'},
    {'type': 'foreach_assert'},
])
def test_appium_preflight_requires_explicit_targets_for_visual_actions(appium_scene, step):
    scene = appium_scene
    scene.case.ui_flow = [step]
    scene.case.save(update_fields=['ui_flow'])

    report = check_appium_prerequisites(scene.case, user=scene.owner)

    assert report['ready'] is False
    assert any(issue['code'] == 'explicit_target_required' for issue in report['issues'])


@pytest.mark.django_db
def test_appium_preflight_keeps_custom_scope_explicit(appium_scene):
    scene = appium_scene
    scene.case.ui_flow = [{
        'kind': 'custom', 'type': 'legacy_local_custom',
        'steps': [{'type': 'click', 'selector_type': 'pos', 'selector': '1,2'}],
    }]
    scene.case.save(update_fields=['ui_flow'])

    report = check_appium_prerequisites(scene.case, user=scene.owner)

    assert report['ready'] is False
    assert any(issue['code'] == 'unsupported_action' for issue in report['issues'])


@pytest.mark.django_db
def test_appium_preflight_rejects_unsupported_and_locatorless_actions(appium_scene):
    scene = appium_scene
    scene.case.ui_flow = [{'type': 'unsupported_native_action'}]
    scene.case.save(update_fields=['ui_flow'])
    report = check_appium_prerequisites(scene.case, user=scene.owner)
    assert report['ready'] is False
    assert any(issue['code'] == 'unsupported_action' for issue in report['issues'])

    scene.case.ui_flow = [{'type': 'image_exists_click', 'element_id': scene.element.pk}]
    scene.case.save(update_fields=['ui_flow'])
    report = check_appium_prerequisites(scene.case, user=scene.owner)
    assert report['ready'] is False
    assert any(issue['code'] == 'unsupported_element_reference' for issue in report['issues'])

    scene.case.ui_flow = [{'type': 'click'}]
    scene.case.save(update_fields=['ui_flow'])
    report = check_appium_prerequisites(scene.case, user=scene.owner)
    assert report['ready'] is False
    assert any(issue['code'] == 'native_element_required' for issue in report['issues'])

    scene.case.ui_flow = [{'type': 'click', 'target_element_id': scene.element.pk}]
    scene.case.save(update_fields=['ui_flow'])
    report = check_appium_prerequisites(scene.case, user=scene.owner)
    assert report['ready'] is False
    assert any(issue['code'] == 'unsupported_element_reference' for issue in report['issues'])


@pytest.mark.django_db
def test_appium_prerequisites_endpoint_is_configuration_only(appium_scene):
    scene = appium_scene
    client = APIClient()
    client.force_authenticate(user=scene.owner)
    response = client.get(reverse('app-test-case-appium-prerequisites', args=[scene.case.pk]))
    assert response.status_code == 200
    assert response.data['success'] is True
    assert response.data['data']['ready'] is True
    assert response.data['data']['scope'] == 'case_configuration'


@pytest.mark.django_db
def test_suite_creation_snapshots_mixed_case_engines(appium_scene):
    scene = appium_scene
    legacy_case = AppTestCase.objects.create(
        project=scene.project,
        app_package=scene.package,
        name='legacy suite member',
        engine=ExecutionEngine.LEGACY,
        ui_flow=[{'type': 'image_exists_click', 'selector': 'legacy.png'}],
    )
    suite = AppTestSuite.objects.create(project=scene.project, name='mixed engines')
    AppTestSuiteCase.objects.create(test_suite=suite, test_case=scene.case, order=1)
    AppTestSuiteCase.objects.create(test_suite=suite, test_case=legacy_case, order=2)
    device = AppDevice.objects.create(device_id='mixed-engine-device', status='online')
    device.lock(scene.owner)

    client = APIClient()
    client.force_authenticate(user=scene.owner)
    with patch('apps.app_automation.views.suite_views.enqueue_app_task'):
        response = client.post(
            reverse('app-test-suite-run', args=[suite.pk]),
            {'device_id': device.device_id},
            format='json',
        )

    assert response.status_code == 200, response.data
    snapshots = {
        execution.test_case_id: execution.engine
        for execution in AppTestExecution.objects.filter(test_suite=suite)
    }
    assert snapshots == {
        scene.case.pk: ExecutionEngine.APPIUM,
        legacy_case.pk: ExecutionEngine.LEGACY,
    }


@pytest.mark.django_db
def test_appium_worker_preflights_before_claiming_or_device_io(appium_scene):
    from apps.app_automation.tasks import execute_app_test_task

    scene = appium_scene
    scene.case.ui_flow = [{'type': 'unsupported_native_action'}]
    scene.case.save(update_fields=['ui_flow'])

    with patch('apps.app_automation.execution_admission.claim_app_execution_batch') as claim:
        execute_app_test_task.run(scene.execution.pk)

    claim.assert_not_called()
    scene.execution.refresh_from_db()
    assert scene.execution.status == 'error'


@pytest.mark.django_db
def test_appium_suite_preflights_every_engine_before_claiming(appium_scene):
    from apps.app_automation.tasks import execute_app_suite_task

    scene = appium_scene
    scene.case.ui_flow = [{'type': 'swipe'}]
    scene.case.save(update_fields=['ui_flow'])
    suite = AppTestSuite.objects.create(project=scene.project, name='invalid appium suite')
    scene.execution.test_suite = suite
    scene.execution.task_id = 'appium-suite-batch'
    scene.execution.save(update_fields=['test_suite', 'task_id'])
    legacy_case = AppTestCase.objects.create(
        project=scene.project,
        name='legacy suite member',
        engine=ExecutionEngine.LEGACY,
    )
    legacy_execution = AppTestExecution.objects.create(
        test_case=legacy_case,
        test_suite=suite,
        user=scene.owner,
        task_id='appium-suite-batch',
    )

    with patch('apps.app_automation.execution_admission.claim_app_execution_batch') as claim:
        execute_app_suite_task.run(
            suite_id=suite.pk,
            execution_ids=[scene.execution.pk, legacy_execution.pk],
            dispatch_id='appium-suite-batch',
        )

    claim.assert_not_called()
    scene.execution.refresh_from_db()
    legacy_execution.refresh_from_db()
    assert scene.execution.status == 'error'
    assert legacy_execution.status == 'error'


@pytest.mark.django_db
def test_single_case_api_creation_snapshots_case_engine(appium_scene):
    scene = appium_scene
    device = AppDevice.objects.create(device_id='single-engine-device', status='online')
    client = APIClient()
    client.force_authenticate(user=scene.owner)

    with (
        patch(
            'apps.app_automation.views.test_case_views.prepare_execution_device',
            return_value=(device, 3, False),
        ),
        patch('apps.app_automation.views.test_case_views.enqueue_app_task'),
    ):
        response = client.post(
            reverse('app-test-case-execute', args=[scene.case.pk]),
            {'device_id': device.device_id},
            format='json',
        )

    assert response.status_code == 200, response.data
    execution = AppTestExecution.objects.get(pk=response.data['execution']['id'])
    assert execution.engine == ExecutionEngine.APPIUM


@pytest.mark.django_db
def test_manual_schedule_creation_snapshots_case_engine(appium_scene):
    scene = appium_scene
    device = AppDevice.objects.create(device_id='scheduled-engine-device', status='online')
    scheduled = AppScheduledTask.objects.create(
        name='appium manual schedule',
        project=scene.project,
        task_type='TEST_CASE',
        trigger_type='INTERVAL',
        interval_seconds=60,
        device=device,
        app_package=scene.package,
        test_case=scene.case,
        created_by=scene.owner,
    )
    client = APIClient()
    client.force_authenticate(user=scene.owner)

    with (
        patch('apps.app_automation.views.scheduled_task_views.is_device_lease_active', return_value=False),
        patch(
            'apps.app_automation.views.scheduled_task_views.acquire_specific_device',
            return_value=(device, 'test-only-lease'),
        ),
        patch('apps.app_automation.views.scheduled_task_views.assert_device_execution_lease'),
        patch('apps.app_automation.views.scheduled_task_views.assert_device_execution_idle'),
        patch('apps.app_automation.views.scheduled_task_views.enqueue_app_task'),
    ):
        response = client.post(reverse('app-scheduled-task-run-now', args=[scheduled.pk]))

    assert response.status_code == 200, response.data
    execution = AppTestExecution.objects.get(task_id=response.data['data']['task_id'])
    assert execution.engine == ExecutionEngine.APPIUM


@pytest.mark.django_db
def test_unified_source_creation_snapshots_case_engine(appium_scene):
    from apps.executions.source_workers import _app_execute

    scene = appium_scene
    device = AppDevice.objects.create(device_id='unified-engine-device', status='online')
    binding = SimpleNamespace(metadata={'approved': True})
    reference = SimpleNamespace(kind='case')

    def complete_execution(execution_id, **_kwargs):
        AppTestExecution.objects.filter(pk=execution_id).update(status='completed', result='passed')

    with (
        patch('apps.app_automation.device_pool.acquire_device', return_value=(device, 'test-only-lease')),
        patch('apps.app_automation.device_pool.assert_device_execution_lease'),
        patch('apps.app_automation.execution_admission.assert_device_execution_idle'),
        patch('apps.app_automation.device_pool.release_device'),
        patch(
            'apps.app_automation.tasks.execute_app_test_task.run',
            side_effect=complete_execution,
        ),
    ):
        success, result = _app_execute(scene.case, reference, binding, scene.owner)

    assert success is True
    execution = AppTestExecution.objects.get(pk=result['execution_id'])
    assert execution.engine == ExecutionEngine.APPIUM
    assert execution.task_id
