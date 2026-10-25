from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient, APIRequestFactory

from apps.ui_automation.models import (
    Element,
    AICase,
    AIExecutionRecord,
    LocatorStrategy,
    TestCase as UiTestCase,
    TestScript as UiTestScript,
    TestSuite as UiTestSuite,
    TestSuiteScript as UiTestSuiteScript,
    TestSuiteTestCase as UiTestSuiteTestCase,
    UiProject,
)
from apps.ui_automation.views import AICaseViewSet, AIExecutionRecordViewSet, STOP_SIGNALS
from apps.ui_automation.serializers import (
    ElementSerializer,
    TestCaseExecutionSerializer as UiTestCaseExecutionSerializer,
    TestCaseStepSerializer as UiTestCaseStepSerializer,
    TestSuiteScriptSerializer as UiTestSuiteScriptSerializer,
    AIExecutionRecordSerializer as UiAIExecutionRecordSerializer,
)


User = get_user_model()


class UiAuthorizationBoundaryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="ui-owner", password="pass")
        self.member = User.objects.create_user(username="ui-member", password="pass")
        self.outsider = User.objects.create_user(username="ui-outsider", password="pass")
        self.project = UiProject.objects.create(
            name="ui-owned", base_url="https://owned.example.test", owner=self.owner
        )
        self.project.members.add(self.member)
        self.other_project = UiProject.objects.create(
            name="ui-other", base_url="https://other.example.test", owner=self.outsider
        )
        self.strategy = LocatorStrategy.objects.create(name="css")
        self.own_element = Element.objects.create(
            project=self.project,
            name="own-button",
            element_type="BUTTON",
            locator_strategy=self.strategy,
            locator_value="#own",
            created_by=self.owner,
        )
        self.other_element = Element.objects.create(
            project=self.other_project,
            name="other-button",
            element_type="BUTTON",
            locator_strategy=self.strategy,
            locator_value="#other",
            created_by=self.outsider,
        )
        self.own_script = UiTestScript.objects.create(
            project=self.project, name="own-script", content="return true"
        )
        self.other_script = UiTestScript.objects.create(
            project=self.other_project, name="other-script", content="return true"
        )
        self.own_case = UiTestCase.objects.create(
            project=self.project, name="own-case", created_by=self.owner
        )
        self.other_case = UiTestCase.objects.create(
            project=self.other_project, name="other-case", created_by=self.outsider
        )
        self.own_suite = UiTestSuite.objects.create(project=self.project, name="own-suite")
        UiTestSuiteTestCase.objects.create(test_suite=self.own_suite, test_case=self.own_case)
        self.factory = APIRequestFactory()

    def request(self, user):
        request = self.factory.post("/api/ui-automation/test-cases/")
        request.user = user
        return request

    def test_element_serializer_denies_foreign_project_and_accepts_member_project(self):
        foreign_data = {
            "project_id": self.other_project.id,
            "name": "forbidden",
            "element_type": "BUTTON",
            "locator_strategy_id": self.strategy.id,
            "locator_value": "#forbidden",
        }
        denied = ElementSerializer(data=foreign_data, context={"request": self.request(self.owner)})
        self.assertFalse(denied.is_valid())

        allowed_data = dict(foreign_data, project_id=self.project.id, name="allowed")
        allowed = ElementSerializer(data=allowed_data, context={"request": self.request(self.member)})
        self.assertTrue(allowed.is_valid(), allowed.errors)

    def test_suite_script_relation_requires_same_project(self):
        denied = UiTestSuiteScriptSerializer(
            data={"test_suite_id": self.own_suite.id, "test_script_id": self.other_script.id},
            context={"request": self.request(self.owner)},
        )
        self.assertFalse(denied.is_valid())

        allowed = UiTestSuiteScriptSerializer(
            data={"test_suite_id": self.own_suite.id, "test_script_id": self.own_script.id},
            context={"request": self.request(self.member)},
        )
        self.assertTrue(allowed.is_valid(), allowed.errors)
        relation = allowed.save()
        self.assertEqual(relation.test_suite_id, self.own_suite.id)

    def test_projectless_ai_records_are_creator_scoped(self):
        ai_case = AICase.objects.create(
            name="private-ai", task_description="private task", created_by=self.owner
        )
        record = AIExecutionRecord.objects.create(
            case_name="private-ai", task_description="private task", executed_by=self.owner
        )

        owner_request = self.factory.get("/api/ui-automation/ai-cases/")
        owner_request.user = self.owner
        owner_view = AICaseViewSet()
        owner_view.request = owner_request
        self.assertIn(ai_case, owner_view.get_queryset())

        outsider_request = self.factory.get("/api/ui-automation/ai-cases/")
        outsider_request.user = self.outsider
        outsider_view = AICaseViewSet()
        outsider_view.request = outsider_request
        self.assertNotIn(ai_case, outsider_view.get_queryset())

        outsider_records = APIRequestFactory().get("/api/ui-automation/ai-execution-records/")
        outsider_records.user = self.outsider
        record_view = AIExecutionRecordViewSet()
        record_view.request = outsider_records
        self.assertNotIn(record, record_view.get_queryset())

        STOP_SIGNALS[record.id] = False
        client = APIClient()
        client.force_authenticate(user=self.outsider)
        denied_stop = client.post(f"/api/ui-automation/ai-execution-records/{record.id}/stop/")
        self.assertEqual(denied_stop.status_code, 404)
        self.assertFalse(STOP_SIGNALS[record.id])
        client.force_authenticate(user=self.owner)
        allowed_stop = client.post(f"/api/ui-automation/ai-execution-records/{record.id}/stop/")
        self.assertEqual(allowed_stop.status_code, 200)
        self.assertTrue(STOP_SIGNALS.pop(record.id))

    def test_projectless_ai_execution_cannot_reference_another_users_case(self):
        ai_case = AICase.objects.create(
            name="private-ai", task_description="private task", created_by=self.owner
        )
        data = {
            "project_id": None,
            "ai_case_id": ai_case.id,
            "case_name": ai_case.name,
            "task_description": ai_case.task_description,
        }
        serializer = UiAIExecutionRecordSerializer(
            data=data,
            context={"request": self.request(self.outsider)},
        )
        self.assertFalse(serializer.is_valid())

    def test_case_step_and_execution_relations_require_same_project(self):
        denied_step = UiTestCaseStepSerializer(
            data={
                "test_case": self.own_case.id,
                "step_number": 1,
                "action_type": "click",
                "element": self.other_element.id,
            },
            context={"request": self.request(self.owner)},
        )
        self.assertFalse(denied_step.is_valid())

        allowed_step = UiTestCaseStepSerializer(
            data={
                "test_case": self.own_case.id,
                "step_number": 1,
                "action_type": "click",
                "element": self.own_element.id,
            },
            context={"request": self.request(self.member)},
        )
        self.assertTrue(allowed_step.is_valid(), allowed_step.errors)

        denied_execution = UiTestCaseExecutionSerializer(
            data={"project": self.project.id, "test_case": self.other_case.id},
            context={"request": self.request(self.owner)},
        )
        self.assertFalse(denied_execution.is_valid())

        allowed_execution = UiTestCaseExecutionSerializer(
            data={"project": self.project.id, "test_case": self.own_case.id},
            context={"request": self.request(self.member)},
        )
        self.assertTrue(allowed_execution.is_valid(), allowed_execution.errors)
