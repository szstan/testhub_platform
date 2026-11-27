from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from apps.app_automation.element_reference_service import reference_summary_for_ids
from apps.app_automation.models import (
    AppCustomComponent,
    AppElement,
    AppProject,
    AppProjectComponent,
    AppTestCase,
)
from apps.app_automation.views.element_views import AppElementViewSet


User = get_user_model()
ELEMENTS_URL = "/api/app-automation/elements/"


class AppElementArchivalTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="element-owner", password="pass")
        self.outsider = User.objects.create_user(username="element-outsider", password="pass")
        self.project = AppProject.objects.create(name="element-project", owner=self.owner)
        self.other_project = AppProject.objects.create(name="other-element-project", owner=self.outsider)
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def make_element(self, name="button", *, project=None, **overrides):
        values = {
            "name": name,
            "element_type": "image",
            "project": self.project if project is None else project,
            "config": {"image_path": "legacy/shared.png"},
        }
        values.update(overrides)
        return AppElement.objects.create(**values)

    def test_list_separates_saved_reference_count_from_runtime_usage_count(self):
        element = self.make_element(usage_count=7)
        AppTestCase.objects.create(
            name="nested reference case",
            project=self.project,
            created_by=self.owner,
            ui_flow=[
                {"type": "if", "then_steps": [{"config": {"selector": {"element_id": element.pk}}}]},
                {"type": "input", "element_id": str(element.pk)},
            ],
        )
        AppProjectComponent.objects.create(
            project=self.project,
            name="v1 component",
            type="saved-element-component",
            version="1",
            steps=[{"type": "click", "config": {"element_id": element.pk}}],
        )
        AppProjectComponent.objects.create(
            project=self.project,
            name="v2 component",
            type="saved-element-component",
            version="2",
            steps=[{"type": "wait", "config": {"selector": {"element_id": element.pk}}}],
        )
        AppCustomComponent.objects.create(
            name="global element component",
            type="global-element-component",
            steps=[{"type": "click", "element_id": element.pk}],
        )

        response = self.client.get(ELEMENTS_URL)

        self.assertEqual(response.status_code, 200, response.data)
        row = next(item for item in response.data["results"] if item["id"] == element.pk)
        self.assertEqual(row["usage_count"], 7)
        self.assertEqual(row["reference_count"], 5)
        self.assertFalse(row["reference_count_unknown"])

    def test_nested_and_versioned_references_block_archive(self):
        element = self.make_element()
        AppTestCase.objects.create(
            name="nested reference case",
            project=self.project,
            created_by=self.owner,
            ui_flow={"steps": [{"type": "try", "try_steps": [{"element_id": element.pk}]}]},
        )
        AppProjectComponent.objects.create(
            project=self.project,
            name="component version",
            type="versioned-reference",
            version="1",
            steps=[{"type": "click", "config": {"selector": {"element_id": element.pk}}}],
        )

        response = self.client.delete(f"{ELEMENTS_URL}{element.pk}/")

        self.assertEqual(response.status_code, 409, response.data)
        element.refresh_from_db()
        self.assertTrue(element.is_active)

    def test_dynamic_reference_is_reported_as_unknown_and_blocks_archive(self):
        element = self.make_element()
        AppTestCase.objects.create(
            name="dynamic element case",
            project=self.project,
            created_by=self.owner,
            ui_flow=[{"type": "click", "element_id": "{{global.current_element}}"}],
        )

        summary = reference_summary_for_ids([element], self.owner)[element.pk]
        response = self.client.delete(f"{ELEMENTS_URL}{element.pk}/")

        self.assertTrue(summary["reference_count_unknown"])
        self.assertTrue(summary["is_referenced"])
        self.assertEqual(summary["reference_count"], 0)
        self.assertEqual(response.status_code, 409, response.data)

    def test_inaccessible_project_reference_is_not_counted_but_still_protects(self):
        element = self.make_element()
        AppTestCase.objects.create(
            name="foreign reference case",
            project=self.other_project,
            created_by=self.outsider,
            ui_flow=[{"type": "click", "element_id": element.pk}],
        )

        summary = reference_summary_for_ids([element], self.owner)[element.pk]
        response = self.client.delete(f"{ELEMENTS_URL}{element.pk}/")

        self.assertEqual(summary["reference_count"], 0)
        self.assertFalse(summary["reference_count_unknown"])
        self.assertTrue(summary["is_referenced"])
        self.assertEqual(response.status_code, 409, response.data)
        self.assertNotIn("reference_count", response.data)

    def test_hidden_dynamic_reference_protects_without_exposing_its_scope(self):
        element = self.make_element()
        AppProjectComponent.objects.create(
            project=self.other_project,
            name="foreign dynamic component",
            type="foreign-dynamic-reference",
            version="1",
            steps=[{"type": "click", "element_id": "{{global.target_element}}"}],
        )

        summary = reference_summary_for_ids([element], self.owner)[element.pk]
        response = self.client.delete(f"{ELEMENTS_URL}{element.pk}/")

        self.assertEqual(summary["reference_count"], 0)
        self.assertFalse(summary["reference_count_unknown"])
        self.assertTrue(summary["is_referenced"])
        self.assertEqual(response.status_code, 409, response.data)
        self.assertNotIn("reference_count", response.data)

    def test_bulk_archive_is_all_or_nothing_when_one_element_is_referenced(self):
        unreferenced = self.make_element("unreferenced")
        referenced = self.make_element("referenced")
        AppTestCase.objects.create(
            name="reference case",
            project=self.project,
            created_by=self.owner,
            ui_flow=[{"type": "click", "element_id": referenced.pk}],
        )

        response = self.client.post(
            f"{ELEMENTS_URL}bulk-archive/",
            {"ids": [unreferenced.pk, referenced.pk]},
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        unreferenced.refresh_from_db()
        referenced.refresh_from_db()
        self.assertTrue(unreferenced.is_active)
        self.assertTrue(referenced.is_active)

    def test_bulk_archive_soft_archives_and_preserves_shared_image_for_preview(self):
        source = self.make_element("source")
        second_source = self.make_element("second source")
        duplicate = self.make_element("duplicate")
        self.client.force_authenticate(user=self.outsider)
        hidden = self.client.delete(f"{ELEMENTS_URL}{source.pk}/")
        self.assertEqual(hidden.status_code, 404)
        source.refresh_from_db()
        self.assertTrue(source.is_active)

        self.client.force_authenticate(user=self.owner)
        with TemporaryDirectory() as tmpdir:
            template_base = Path(tmpdir)
            image = template_base / "legacy" / "shared.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"shared-image-bytes")

            with patch.object(AppElementViewSet, "get_template_base_path", return_value=template_base):
                response = self.client.post(
                    f"{ELEMENTS_URL}bulk-archive/",
                    {"ids": [source.pk, second_source.pk]},
                    format="json",
                )
                preview = self.client.get(f"{ELEMENTS_URL}{duplicate.pk}/preview/")

            self.assertEqual(response.status_code, 200, response.data)
            self.assertFalse(AppElement.objects.get(pk=source.pk).is_active)
            self.assertFalse(AppElement.objects.get(pk=second_source.pk).is_active)
            self.assertTrue(image.is_file())
            self.assertEqual(preview.status_code, 200)
            preview_content = b"".join(preview.streaming_content)
            preview.close()
            self.assertEqual(preview_content, b"shared-image-bytes")

    def test_sqlite_search_falls_back_to_name_without_json_contains(self):
        if connection.vendor != "sqlite":
            self.skipTest("SQLite JSON tag lookup fallback check")
        name_match = self.make_element("name-needle-match", tags=[])
        self.make_element("unrelated-tagged", tags=["needle"])

        response = self.client.get(ELEMENTS_URL, {"search": "needle"})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data["results"]], [name_match.pk])

    def test_mysql_search_keeps_exact_json_tag_match(self):
        if connection.vendor != "mysql":
            self.skipTest("MySQL JSON_CONTAINS integration check")
        exact = self.make_element("exact-tag-row", tags=["needle"])
        self.make_element("substring-tag-row", tags=["needle-suffix"])

        response = self.client.get(ELEMENTS_URL, {"search": "needle"})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data["results"]], [exact.pk])
