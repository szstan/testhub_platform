from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient, APIRequestFactory
from types import SimpleNamespace
from unittest.mock import patch

from apps.app_automation.models import (
    AppDevice,
    AppPackage,
    AppProject,
    AppTestCase,
    AppTestSuite,
    AppTestSuiteCase,
)
from apps.app_automation.serializers import AppScheduledTaskSerializer


User = get_user_model()


class AppAuthorizationBoundaryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="app-scope-owner", password="pass")
        self.member = User.objects.create_user(username="app-scope-member", password="pass")
        self.outsider = User.objects.create_user(username="app-scope-outsider", password="pass")
        self.project = AppProject.objects.create(name="app-owned", owner=self.owner)
        self.project.members.add(self.member)
        self.other_project = AppProject.objects.create(name="app-other", owner=self.outsider)
        self.own_package = AppPackage.objects.create(
            name="Owned App", package_name="com.example.owned", created_by=self.owner
        )
        self.other_package = AppPackage.objects.create(
            name="Other App", package_name="com.example.other", created_by=self.outsider
        )
        self.own_case = AppTestCase.objects.create(
            project=self.project,
            name="owned-case",
            app_package=self.own_package,
            created_by=self.owner,
        )
        self.other_case = AppTestCase.objects.create(
            project=self.other_project,
            name="other-case",
            app_package=self.other_package,
            created_by=self.outsider,
        )
        self.own_suite = AppTestSuite.objects.create(
            project=self.project, name="owned-suite", created_by=self.owner
        )
        AppTestSuiteCase.objects.create(test_suite=self.own_suite, test_case=self.own_case)
        self.other_suite = AppTestSuite.objects.create(
            project=self.other_project, name="other-suite", created_by=self.outsider
        )
        AppTestSuiteCase.objects.create(test_suite=self.other_suite, test_case=self.other_case)
        self.factory = APIRequestFactory()

    def request(self, user):
        request = self.factory.post("/api/app-automation/scheduled-tasks/")
        request.user = user
        return request

    def task_data(self, **overrides):
        data = {
            "name": "scheduled",
            "project": self.project.id,
            "task_type": "TEST_SUITE",
            "trigger_type": "INTERVAL",
            "interval_seconds": 60,
            "test_suite": self.own_suite.id,
            "app_package": self.own_package.id,
        }
        data.update(overrides)
        return data

    def test_scheduled_task_rejects_foreign_suite_and_package(self):
        serializer = AppScheduledTaskSerializer(
            data=self.task_data(test_suite=self.other_suite.id, app_package=self.other_package.id),
            context={"request": self.request(self.owner)},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("test_suite", serializer.errors)

    def test_scheduled_task_rejects_other_users_locked_device(self):
        device = AppDevice.objects.create(
            device_id="foreign-locked", status="locked", locked_by=self.outsider
        )
        serializer = AppScheduledTaskSerializer(
            data=self.task_data(device=device.id),
            context={"request": self.request(self.owner)},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("device", serializer.errors)

        serializer = AppScheduledTaskSerializer(
            data=self.task_data(),
            context={"request": self.request(self.member)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_locked_device_is_hidden_from_unrelated_user(self):
        device = AppDevice.objects.create(
            device_id="locked-device", name="locked", status="locked", locked_by=self.owner
        )
        client = APIClient()
        client.force_authenticate(user=self.outsider)
        response = client.get(f"/api/app-automation/devices/{device.id}/")
        self.assertEqual(response.status_code, 404)

        client.force_authenticate(user=self.owner)
        response = client.get(f"/api/app-automation/devices/{device.id}/")
        self.assertEqual(response.status_code, 200)

    def test_screenshot_action_is_routed_and_scoped(self):
        device = AppDevice.objects.create(
            device_id='screenshot-device', status='online', locked_by=self.owner,
        )
        client = APIClient()
        client.force_authenticate(user=self.owner)
        with patch(
            'apps.app_automation.views.device_views.subprocess.run',
            return_value=SimpleNamespace(stdout=b'png-bytes'),
        ):
            response = client.post(
                f'/api/app-automation/devices/{device.id}/screenshot/'
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['data']['content'].startswith('data:image/png;base64,'))

        client.force_authenticate(user=self.outsider)
        denied = client.post(f'/api/app-automation/devices/{device.id}/screenshot/')
        self.assertEqual(denied.status_code, 404)


        client = APIClient()
        payload = {
            "name": "Login Component",
            "type": "login_component",
            "schema": {},
            "default_config": {},
            "steps": [],
        }
        client.force_authenticate(user=self.owner)
        denied = client.post("/api/app-automation/custom-components/", payload, format="json")
        self.assertEqual(denied.status_code, 403)

        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        client.force_authenticate(user=self.owner)
        allowed = client.post("/api/app-automation/custom-components/", payload, format="json")
        self.assertEqual(allowed.status_code, 201, allowed.data)
