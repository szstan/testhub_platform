from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.defects.models import Defect, DefectCodeSequence, DefectTransition
from apps.projects.models import Project, ProjectMember
from apps.users.models import User


class DefectSecurityRegressionTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='defect-owner', phone='11111111111')
        self.viewer = User.objects.create_user(username='defect-viewer')
        self.developer = User.objects.create_user(username='defect-developer')
        self.project = Project.objects.create(name='Defect project', owner=self.owner)
        ProjectMember.objects.create(project=self.project, user=self.viewer, role='viewer')
        ProjectMember.objects.create(project=self.project, user=self.developer, role='developer')
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def create_payload(self, project=None):
        return {
            'title': 'Defect regression',
            'project_id': (project or self.project).pk,
        }

    def test_viewer_cannot_update_visible_defect(self):
        defect = Defect.objects.create(
            title='Visible but read-only', project=self.project, reporter=self.owner,
        )
        self.client.force_authenticate(self.viewer)

        response = self.client.patch(
            reverse('defects-detail', args=[defect.pk]),
            {'title': 'Changed by viewer'},
            format='json',
        )
        deleted = self.client.delete(reverse('defects-detail', args=[defect.pk]))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(deleted.status_code, status.HTTP_403_FORBIDDEN)
        defect.refresh_from_db()
        self.assertEqual(defect.title, 'Visible but read-only')

    def test_project_viewer_cannot_use_defect_self_role_exception(self):
        defect = Defect.objects.create(
            title='Viewer reporter', project=self.project, reporter=self.viewer,
            status='verified',
        )
        self.client.force_authenticate(self.viewer)

        response = self.client.post(
            reverse('defects-close', args=[defect.pk]), {}, format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        defect.refresh_from_db()
        self.assertEqual(defect.status, 'verified')
        self.assertFalse(defect.transitions.exists())

    def test_update_requires_write_access_to_both_source_and_destination(self):
        mover = User.objects.create_user(username='defect-mover')
        destination = Project.objects.create(name='Writable destination', owner=self.owner)
        ProjectMember.objects.create(project=self.project, user=mover, role='viewer')
        ProjectMember.objects.create(project=destination, user=mover, role='developer')
        defect = Defect.objects.create(
            title='Source-scoped defect', project=self.project, reporter=self.owner,
        )
        self.client.force_authenticate(mover)

        response = self.client.patch(
            reverse('defects-detail', args=[defect.pk]),
            {'project_id': destination.pk},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        defect.refresh_from_db()
        self.assertEqual(defect.project_id, self.project.pk)

    def test_trend_days_requires_integer_between_one_and_365(self):
        url = reverse('defects-trend')
        for value in ('not-a-number', '0', '-1', '366'):
            with self.subTest(days=value):
                response = self.client.get(url, {'days': value})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        one_day = self.client.get(url, {'days': '1'})
        self.assertEqual(one_day.status_code, status.HTTP_200_OK)
        self.assertEqual(len(one_day.data), 1)
        max_days = self.client.get(url, {'days': '365'})
        self.assertEqual(max_days.status_code, status.HTTP_200_OK)
        self.assertEqual(len(max_days.data), 365)

    def test_generated_number_avoids_existing_numeric_suffix_conflict(self):
        today = timezone.localdate()
        prefix = f'BUG-{today:%Y%m%d}-'
        Defect.objects.create(
            code=f'{prefix}9999', title='Legacy high number',
            project=self.project, reporter=self.owner,
        )

        generated = Defect.objects.create(
            title='Generated after legacy high number',
            project=self.project,
            reporter=self.owner,
        )
        next_generated = Defect.objects.create(
            title='Next generated number',
            project=self.project,
            reporter=self.owner,
        )

        self.assertEqual(generated.code, f'{prefix}10000')
        self.assertEqual(next_generated.code, f'{prefix}10001')
        self.assertEqual(DefectCodeSequence.objects.get(day=today).last_number, 10001)

    def test_create_and_initial_transition_roll_back_together(self):
        defect_count = Defect.objects.count()
        sequence_count = DefectCodeSequence.objects.count()
        today = timezone.localdate()

        with patch(
            'apps.defects.views.DefectTransition.objects.create',
            side_effect=RuntimeError('audit write failed'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('defects-list'), self.create_payload(), format='json',
                )

        self.assertEqual(Defect.objects.count(), defect_count)
        self.assertEqual(DefectTransition.objects.count(), 0)
        self.assertEqual(DefectCodeSequence.objects.count(), sequence_count)
        self.assertFalse(DefectCodeSequence.objects.filter(day=today).exists())

    def test_normal_number_allocation_does_not_scan_daily_history(self):
        Defect.objects.create(title='First number', project=self.project, reporter=self.owner)
        with CaptureQueriesContext(connection) as queries:
            Defect.objects.create(title='Second number', project=self.project, reporter=self.owner)
        self.assertFalse(any(' LIKE ' in query['sql'].upper() for query in queries))

    def test_allocator_recovers_an_explicit_legacy_code_collision(self):
        first = Defect.objects.create(title='First', project=self.project, reporter=self.owner)
        prefix = first.code.rsplit('-', 1)[0] + '-'
        Defect.objects.create(code=prefix + '0002', title='Manual legacy code', project=self.project, reporter=self.owner)
        generated = Defect.objects.create(title='After collision', project=self.project, reporter=self.owner)
        self.assertEqual(generated.code, prefix + '0003')

    def test_nested_user_data_uses_directory_serializer(self):
        defect = Defect.objects.create(
            title='Private user fields', project=self.project, reporter=self.owner,
        )
        response = self.client.get(reverse('defects-detail', args=[defect.pk]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('email', response.data['reporter'])
        self.assertNotIn('phone', response.data['reporter'])
        self.assertNotIn('is_staff', response.data['reporter'])


@skipUnlessDBFeature('has_select_for_update')
class DefectNumberConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.owner = User.objects.create_user(username='concurrent-defect-owner')
        self.project = Project.objects.create(name='Concurrent defect project', owner=self.owner)

    def test_concurrent_creates_receive_distinct_daily_numbers(self):
        barrier = Barrier(2)

        def create_defect(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                defect = Defect.objects.create(
                    title=f'Concurrent defect {index}',
                    project_id=self.project.pk,
                    reporter_id=self.owner.pk,
                )
                return defect.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(create_defect, range(2)))

        self.assertEqual(len(set(codes)), 2)
        self.assertEqual(Defect.objects.count(), 2)


def test_defect_sequence_migration_matches_live_models():
    from django.apps import apps
    from django.db.migrations.autodetector import MigrationAutodetector
    from django.db.migrations.loader import MigrationLoader
    from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
    from django.db.migrations.state import ProjectState
    from django.test import override_settings

    with override_settings(MIGRATION_MODULES={}):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        state = loader.project_state([('defects', '0004_defect_code_sequence')])
        assert state.apps.get_model('defects', 'DefectCodeSequence')._meta.db_table == 'defect_code_sequences'
        changes = MigrationAutodetector(
            loader.project_state(), ProjectState.from_apps(apps),
            questioner=NonInteractiveMigrationQuestioner(),
        ).changes(graph=loader.graph, trim_to_apps={'defects'})
        assert not changes.get('defects'), changes.get('defects')
