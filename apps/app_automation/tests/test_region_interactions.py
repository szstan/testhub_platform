from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

pytest.importorskip('airtest.core.api', reason='APP worker Airtest dependency is not installed')

from apps.app_automation.models import AppElement
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner


@pytest.mark.django_db
def test_input_uses_center_of_saved_region_element():
    user = get_user_model().objects.create_user(username='region-input-owner')
    element = AppElement.objects.create(
        name='region-input', element_type='region', created_by=user,
        config={'x1': 132, 'y1': 476, 'x2': 307, 'y2': 509},
    )
    runner = UiFlowRunner.__new__(UiFlowRunner)

    with (
        patch('apps.app_automation.runners.ui_flow_runner.touch') as touch,
        patch('apps.app_automation.runners.ui_flow_runner.airtest_text') as text,
        patch('apps.app_automation.runners.ui_flow_runner.time.sleep'),
        patch('apps.core.variable_resolver.resolve_variables', return_value='hello'),
    ):
        runner._action_input({'element_id': element.pk, 'value': 'hello'})

    touch.assert_called_once_with((219, 492))
    text.assert_called_once_with('hello')


@pytest.mark.parametrize(('method', 'target_function'), [
    ('_action_touch', 'touch'),
    ('_action_double_click', 'double_click'),
    ('_action_long_press', 'touch'),
])
def test_point_gestures_use_the_center_of_a_region(method, target_function):
    runner = UiFlowRunner.__new__(UiFlowRunner)
    with (
        patch.object(runner, '_resolve_selector', return_value=(20, 40, 100, 80)),
        patch(f'apps.app_automation.runners.ui_flow_runner.{target_function}') as gesture,
    ):
        getattr(runner, method)({'duration': 2})

    assert gesture.call_args.args[0] == (60, 60)


def test_region_conversion_preserves_points_and_image_templates():
    marker = object()
    assert UiFlowRunner._interaction_target(marker) is marker
    assert UiFlowRunner._interaction_target((10, 20)) == (10, 20)
    assert UiFlowRunner._interaction_target((100, 80, 20, 40)) == (60, 60)


@pytest.mark.parametrize('region', [(1, 2, 1, 10), (None, 2, 10, 20), (1, 2, float('nan'), 20)])
def test_invalid_regions_fail_with_an_actionable_error(region):
    with pytest.raises(ValueError, match='区域坐标无效'):
        UiFlowRunner._interaction_target(region)


def test_wait_rejects_region_instead_of_passing_it_to_image_matching():
    runner = UiFlowRunner.__new__(UiFlowRunner)
    with (
        patch.object(runner, '_resolve_selector', return_value=(10, 20, 30, 40)),
        patch('apps.app_automation.runners.ui_flow_runner.wait') as wait,
        pytest.raises(ValueError, match='等待元素出现需要图片元素'),
    ):
        runner._action_wait({'timeout': 2})
    wait.assert_not_called()


def test_ocr_region_remains_a_four_coordinate_rectangle():
    runner = UiFlowRunner.__new__(UiFlowRunner)
    assert runner._parse_ocr_region({
        'selector_type': 'region', 'selector': '132, 476, 307, 509',
    }) == (132, 476, 307, 509)
