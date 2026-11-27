from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.projects.models import Project, ProjectMember
from apps.users.models import User
from apps.versions.models import Version
from .models import Defect, DefectTransition
from .serializers import DefectWriteSerializer


class DefectApiTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.tester = User.objects.create_user(username='tester', password='pass123456')
        self.developer = User.objects.create_user(username='developer', password='pass123456')
        self.viewer = User.objects.create_user(username='viewer', password='pass123456')
        self.other_user = User.objects.create_user(username='other', password='pass123456')
        self.project = Project.objects.create(name='TestHub', owner=self.tester)
        ProjectMember.objects.create(project=self.project, user=self.developer, role='developer')
        ProjectMember.objects.create(project=self.project, user=self.viewer, role='viewer')
        self.version = Version.objects.create(name='v1.0', created_by=self.tester)
        self.version.projects.add(self.project)
        self.client.force_authenticate(self.tester)
        self.list_url = reverse('defects-list')

    def test_create_defect(self):
        response = self.client.post(self.list_url, self.build_payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['title'], '登录失败')
        self.assertTrue(response.data['code'].startswith('BUG-'))
        self.assertEqual(response.data['status'], 'new')
        self.assertEqual(DefectTransition.objects.count(), 1)

    def test_filter_by_version_module_and_assignee(self):
        defect = Defect.objects.create(
            title='支付失败',
            project=self.project,
            version=self.version,
            module='支付',
            reporter=self.tester,
            assignee=self.developer,
        )
        Defect.objects.create(
            title='无权限数据',
            project=Project.objects.create(name='Other', owner=self.other_user),
            module='支付',
            reporter=self.other_user,
        )

        response = self.client.get(self.list_url, {
            'version': self.version.id,
            'module': '支付',
            'assignee': self.developer.id,
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result_ids = [item['id'] for item in response.data['results']]
        self.assertEqual(result_ids, [defect.id])

    def test_assign_and_close_defect(self):
        defect = Defect.objects.create(title='登录失败', project=self.project, reporter=self.tester)

        assign_url = reverse('defects-assign', args=[defect.id])
        assign_response = self.client.post(assign_url, {'assignee_id': self.developer.id}, format='json')

        self.assertEqual(assign_response.status_code, status.HTTP_200_OK)
        self.assertEqual(assign_response.data['status'], 'assigned')
        defect.refresh_from_db()
        self.assertEqual(defect.assignee_id, self.developer.id)

        defect.status = 'verified'
        defect.save(update_fields=['status'])
        close_url = reverse('defects-close', args=[defect.id])
        close_response = self.client.post(close_url, {'comment': '回归通过'}, format='json')

        self.assertEqual(close_response.status_code, status.HTTP_200_OK)
        self.assertEqual(close_response.data['status'], 'closed')
        defect.refresh_from_db()
        self.assertIsNotNone(defect.closed_at)

    def test_new_defect_cannot_close_directly(self):
        defect = Defect.objects.create(title='登录失败', project=self.project, reporter=self.tester)

        close_url = reverse('defects-close', args=[defect.id])
        close_response = self.client.post(close_url, {'comment': '直接关闭'}, format='json')

        self.assertEqual(close_response.status_code, status.HTTP_400_BAD_REQUEST)
        defect.refresh_from_db()
        self.assertEqual(defect.status, 'new')

    def test_start_progress_then_resolve_defect(self):
        defect = Defect.objects.create(
            title='提交按钮无响应',
            project=self.project,
            reporter=self.tester,
            assignee=self.developer,
            status='assigned',
        )

        self.client.force_authenticate(self.developer)
        start_url = reverse('defects-start-progress', args=[defect.id])
        start_response = self.client.post(start_url, {'comment': '开始定位问题'}, format='json')

        self.assertEqual(start_response.status_code, status.HTTP_200_OK)
        self.assertEqual(start_response.data['status'], 'in_progress')
        defect.refresh_from_db()
        self.assertEqual(defect.resolver_id, self.developer.id)

        resolve_url = reverse('defects-resolve', args=[defect.id])
        resolve_response = self.client.post(resolve_url, {'comment': '已修复'}, format='json')

        self.assertEqual(resolve_response.status_code, status.HTTP_200_OK)
        self.assertEqual(resolve_response.data['status'], 'resolved')
        defect.refresh_from_db()
        self.assertIsNotNone(defect.resolved_at)

    def test_add_comment_and_upload_attachment(self):
        defect = Defect.objects.create(title='登录失败', project=self.project, reporter=self.tester)

        comment_url = reverse('defects-add-comment', args=[defect.id])
        comment_response = self.client.post(comment_url, {'content': '请补充浏览器版本'}, format='json')

        self.assertEqual(comment_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(comment_response.data['content'], '请补充浏览器版本')

        attachment_url = reverse('defects-upload-attachment', args=[defect.id])
        upload_file = SimpleUploadedFile('evidence.txt', b'failed screenshot placeholder', content_type='text/plain')
        attachment_response = self.client.post(
            attachment_url,
            {'name': 'evidence.txt', 'file': upload_file},
            format='multipart',
        )

        self.assertEqual(attachment_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(attachment_response.data['name'], 'evidence.txt')

    def test_upload_attachment_rejects_invalid_extension_and_oversize(self):
        defect = Defect.objects.create(title='登录失败', project=self.project, reporter=self.tester)
        attachment_url = reverse('defects-upload-attachment', args=[defect.id])

        invalid_file = SimpleUploadedFile('script.exe', b'fake-binary', content_type='application/octet-stream')
        invalid_response = self.client.post(
            attachment_url,
            {'name': 'script.exe', 'file': invalid_file},
            format='multipart',
        )
        self.assertEqual(invalid_response.status_code, status.HTTP_400_BAD_REQUEST)

        large_file = SimpleUploadedFile(
            'huge.txt',
            b'a' * (10 * 1024 * 1024 + 1),
            content_type='text/plain',
        )
        large_response = self.client.post(
            attachment_url,
            {'name': 'huge.txt', 'file': large_file},
            format='multipart',
        )
        self.assertEqual(large_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_viewer_cannot_create_or_transition_defect(self):
        self.client.force_authenticate(self.viewer)
        create_response = self.client.post(self.list_url, self.build_payload(), format='json')

        self.assertEqual(create_response.status_code, status.HTTP_403_FORBIDDEN)

        defect = Defect.objects.create(title='登录失败', project=self.project, reporter=self.tester)
        assign_url = reverse('defects-assign', args=[defect.id])
        assign_response = self.client.post(assign_url, {'assignee_id': self.developer.id}, format='json')

        self.assertEqual(assign_response.status_code, status.HTTP_403_FORBIDDEN)

    def test_bulk_actions_enforce_project_roles_and_allow_matching_self_roles(self):
        close_defect = Defect.objects.create(
            title='不能越权关闭',
            project=self.project,
            reporter=self.tester,
            status='verified',
        )
        metadata_defect = Defect.objects.create(
            title='不能越权修改元数据',
            project=self.project,
            reporter=self.tester,
            priority='p2',
        )
        self.client.force_authenticate(self.viewer)

        denied_close = self.client.post(
            reverse('defects-bulk-action'),
            {'action': 'close', 'defect_ids': [close_defect.id]},
            format='json',
        )
        self.assertEqual(denied_close.status_code, status.HTTP_200_OK)
        self.assertFalse(denied_close.data['results'][0]['success'])
        close_defect.refresh_from_db()
        self.assertEqual(close_defect.status, 'verified')
        self.assertFalse(DefectTransition.objects.filter(defect=close_defect).exists())

        denied_priority = self.client.post(
            reverse('defects-bulk-action'),
            {'action': 'set_priority', 'defect_ids': [metadata_defect.id], 'priority': 'p1'},
            format='json',
        )
        self.assertEqual(denied_priority.status_code, status.HTTP_200_OK)
        self.assertFalse(denied_priority.data['results'][0]['success'])
        metadata_defect.refresh_from_db()
        self.assertEqual(metadata_defect.priority, 'p2')

        foreign_project = Project.objects.create(name='Foreign reporter project', owner=self.tester)
        reporter_defect = Defect.objects.create(
            title='提交人可关闭自己的缺陷',
            project=foreign_project,
            reporter=self.other_user,
            status='verified',
        )
        self.client.force_authenticate(self.other_user)
        allowed_close = self.client.post(
            reverse('defects-bulk-action'),
            {'action': 'close', 'defect_ids': [reporter_defect.id]},
            format='json',
        )
        self.assertEqual(allowed_close.status_code, status.HTTP_200_OK)
        self.assertTrue(allowed_close.data['results'][0]['success'])
        reporter_defect.refresh_from_db()
        self.assertEqual(reporter_defect.status, 'closed')

    def test_trend_counts_close_transitions_and_close_retries_are_idempotent(self):
        today = timezone.localdate()
        date_a, date_b, date_c = (today - timedelta(days=offset) for offset in (2, 1, 0))
        close_at_a = self.local_noon(date_a)
        close_at_b = self.local_noon(date_b)
        close_at_c = self.local_noon(date_c)
        defect = Defect.objects.create(
            title='重开后再次关闭',
            project=self.project,
            reporter=self.tester,
            status='verified',
        )
        close_url = reverse('defects-close', args=[defect.id])

        with patch('apps.defects.views.timezone.now', return_value=close_at_a):
            first_close = self.client.post(close_url, {}, format='json')
        self.assertEqual(first_close.status_code, status.HTTP_200_OK)
        defect.refresh_from_db()
        original_closed_at = defect.closed_at

        with patch('apps.defects.views.timezone.now', return_value=close_at_b):
            repeated_close = self.client.post(close_url, {}, format='json')
        self.assertEqual(repeated_close.status_code, status.HTTP_200_OK)
        defect.refresh_from_db()
        self.assertEqual(defect.closed_at, original_closed_at)
        self.assertEqual(
            DefectTransition.objects.filter(defect=defect, to_status='closed').count(),
            1,
        )

        with patch('apps.defects.views.timezone.now', return_value=close_at_b):
            reopen = self.client.post(reverse('defects-reopen', args=[defect.id]), {}, format='json')
        self.assertEqual(reopen.status_code, status.HTTP_200_OK)
        defect.refresh_from_db()
        self.assertIsNone(defect.closed_at)

        trend = self.closed_trend(days=3)
        self.assertEqual([trend[day.strftime('%Y-%m-%d')] for day in (date_a, date_b, date_c)], [1, 0, 0])

        defect.status = 'verified'
        defect.save(update_fields=['status'])
        with patch('apps.defects.views.timezone.now', return_value=close_at_c):
            second_close = self.client.post(close_url, {}, format='json')
        self.assertEqual(second_close.status_code, status.HTTP_200_OK)
        self.assertEqual(
            DefectTransition.objects.filter(defect=defect, to_status='closed', from_status='verified').count(),
            2,
        )
        trend = self.closed_trend(days=3)
        self.assertEqual([trend[day.strftime('%Y-%m-%d')] for day in (date_a, date_b, date_c)], [1, 0, 1])

    def test_bulk_close_retries_are_idempotent(self):
        today = timezone.localdate()
        date_a, date_b = today - timedelta(days=1), today
        close_at_a = self.local_noon(date_a)
        close_at_b = self.local_noon(date_b)
        defect = Defect.objects.create(
            title='批量重复关闭',
            project=self.project,
            reporter=self.tester,
            status='verified',
        )
        url = reverse('defects-bulk-action')
        payload = {'action': 'close', 'defect_ids': [defect.id, defect.id]}

        with patch('apps.defects.views.timezone.now', return_value=close_at_a):
            first_close = self.client.post(url, payload, format='json')
        self.assertEqual(first_close.status_code, status.HTTP_200_OK)
        self.assertEqual([item['success'] for item in first_close.data['results']], [True, True])
        defect.refresh_from_db()
        original_closed_at = defect.closed_at

        with patch('apps.defects.views.timezone.now', return_value=close_at_b):
            repeated_close = self.client.post(
                url,
                {'action': 'close', 'defect_ids': [defect.id]},
                format='json',
            )
        self.assertEqual(repeated_close.status_code, status.HTTP_200_OK)
        defect.refresh_from_db()
        self.assertEqual(defect.closed_at, original_closed_at)
        self.assertEqual(
            DefectTransition.objects.filter(defect=defect, to_status='closed', from_status='verified').count(),
            1,
        )
        trend = self.closed_trend(days=2)
        self.assertEqual([trend[day.strftime('%Y-%m-%d')] for day in (date_a, date_b)], [1, 0])

    def test_trend_preserves_project_scope_and_query_filters(self):
        today = timezone.localdate()
        closed_at = self.local_noon(today)
        other_accessible_project = Project.objects.create(name='另一个可访问项目', owner=self.tester)
        foreign_project = Project.objects.create(name='不可访问项目', owner=self.other_user)
        defects = [
            Defect.objects.create(
                title='匹配项目和严重级别',
                project=self.project,
                reporter=self.tester,
                severity='critical',
                status='closed',
            ),
            Defect.objects.create(
                title='不匹配严重级别',
                project=self.project,
                reporter=self.tester,
                severity='major',
                status='closed',
                closed_at=closed_at,
            ),
            Defect.objects.create(
                title='不匹配项目',
                project=other_accessible_project,
                reporter=self.tester,
                severity='critical',
                status='closed',
                closed_at=closed_at,
            ),
            Defect.objects.create(
                title='不可访问项目数据',
                project=foreign_project,
                reporter=self.other_user,
                severity='critical',
                status='closed',
                closed_at=closed_at,
            ),
        ]
        for defect, from_status in zip(defects, ('imported', 'verified', 'verified', 'verified')):
            DefectTransition.objects.create(
                defect=defect,
                from_status=from_status,
                to_status='closed',
                operator=defect.reporter,
                created_at=closed_at,
            )

        response = self.client.get(reverse('defects-trend'), {
            'days': 1,
            'project': self.project.id,
            'severity': 'critical',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['closed'], 1)

    def test_trend_counts_existing_marked_backfill_transitions(self):
        today = timezone.localdate()
        date_a, date_b = today - timedelta(days=1), today
        defect = Defect.objects.create(
            title='已有明确标记的历史补录',
            project=self.project,
            reporter=self.tester,
            status='reopened',
        )
        DefectTransition.objects.create(
            defect=defect,
            from_status='legacy',
            to_status='closed',
            operator=self.tester,
            comment='[Legacy close timestamp backfill] Original closer unknown.',
            created_at=self.local_noon(date_a),
        )

        trend = self.closed_trend(days=2)
        self.assertEqual([trend[day.strftime('%Y-%m-%d')] for day in (date_a, date_b)], [1, 0])

    def test_legacy_closed_at_fallback_survives_reopen_without_double_counting(self):
        today = timezone.localdate()
        date_a, date_b, date_c = (today - timedelta(days=offset) for offset in (2, 1, 0))
        legacy_closed_at = self.local_noon(date_a)
        defect = Defect.objects.create(
            title='部署前关闭的缺陷',
            project=self.project,
            reporter=self.tester,
            status='closed',
            closed_at=legacy_closed_at,
        )
        trend = self.closed_trend(days=3)
        self.assertEqual([trend[day.strftime('%Y-%m-%d')] for day in (date_a, date_b, date_c)], [1, 0, 0])

        with patch('apps.defects.views.timezone.now', return_value=self.local_noon(date_b)):
            reopen = self.client.post(reverse('defects-reopen', args=[defect.id]), {}, format='json')
        self.assertEqual(reopen.status_code, status.HTTP_200_OK)
        defect.refresh_from_db()
        self.assertIsNone(defect.closed_at)
        legacy_transition = DefectTransition.objects.get(
            defect=defect,
            from_status='',
            to_status='closed',
        )
        self.assertEqual(legacy_transition.created_at, legacy_closed_at)
        self.assertEqual(legacy_transition.operator, self.tester)
        self.assertIn('补录历史关闭时间', legacy_transition.comment)
        self.assertIn('操作人为补录人', legacy_transition.comment)

        trend = self.closed_trend(days=3)
        self.assertEqual([trend[day.strftime('%Y-%m-%d')] for day in (date_a, date_b, date_c)], [1, 0, 0])

        defect.status = 'verified'
        defect.save(update_fields=['status'])
        with patch('apps.defects.views.timezone.now', return_value=self.local_noon(date_c)):
            close = self.client.post(reverse('defects-close', args=[defect.id]), {}, format='json')
        self.assertEqual(close.status_code, status.HTTP_200_OK)
        trend = self.closed_trend(days=3)
        self.assertEqual([trend[day.strftime('%Y-%m-%d')] for day in (date_a, date_b, date_c)], [1, 0, 1])

    def test_trend_combines_history_and_legacy_on_the_same_local_day(self):
        with timezone.override('Asia/Shanghai'):
            today = timezone.localdate()
            previous_day = today - timedelta(days=1)
            closed_at = timezone.make_aware(datetime.combine(previous_day, time(hour=0, minute=15)))
            retry_at = self.local_noon(today)
            Defect.objects.create(
                title='没有流转记录的历史缺陷', project=self.project,
                reporter=self.tester, status='closed', closed_at=closed_at,
            )
            historical = Defect.objects.create(
                title='过去曾被重复关闭的缺陷', project=self.project,
                reporter=self.tester, status='closed', closed_at=retry_at,
            )
            DefectTransition.objects.create(
                defect=historical, from_status='verified', to_status='closed',
                operator=self.tester, created_at=closed_at,
            )
            DefectTransition.objects.create(
                defect=historical, from_status='closed', to_status='closed',
                operator=self.tester, created_at=retry_at,
            )

            self.assertEqual(self.closed_trend(days=2), {
                previous_day.isoformat(): 2,
                today.isoformat(): 0,
            })

    def test_close_state_and_transition_history_roll_back_together(self):
        defect = Defect.objects.create(
            title='历史写入失败时回滚',
            project=self.project,
            reporter=self.tester,
            status='verified',
        )

        with patch('apps.defects.views.DefectTransition.objects.create', side_effect=RuntimeError('history write failed')):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse('defects-close', args=[defect.id]), {}, format='json')

        defect.refresh_from_db()
        self.assertEqual(defect.status, 'verified')
        self.assertIsNone(defect.closed_at)
        self.assertFalse(DefectTransition.objects.filter(defect=defect, to_status='closed').exists())

    def test_transition_returns_not_found_if_defect_is_deleted_before_lock(self):
        defect = Defect.objects.create(
            title='流转期间被删除',
            project=self.project,
            reporter=self.tester,
            status='verified',
        )

        with patch('apps.defects.views.Defect.objects.select_for_update') as select_for_update:
            select_for_update.return_value.get.side_effect = Defect.DoesNotExist
            response = self.client.post(reverse('defects-close', args=[defect.id]), {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['error'], '缺陷不存在')

    @staticmethod
    def local_noon(day):
        return timezone.make_aware(datetime.combine(day, time(hour=12)))

    def closed_trend(self, days):
        response = self.client.get(reverse('defects-trend'), {'days': days})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item['date']: item['closed'] for item in response.data}

    def test_summary(self):
        Defect.objects.create(title='阻塞缺陷', project=self.project, reporter=self.tester, severity='blocker')
        Defect.objects.create(title='已关闭缺陷', project=self.project, reporter=self.tester, status='closed')

        response = self.client.get(reverse('defects-summary'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['total'], 2)
        self.assertEqual(response.data['closed'], 1)
        self.assertEqual(response.data['severe'], 1)

    def test_title_edit_does_not_restore_state_read_before_close(self):
        defect = Defect.objects.create(
            title='编辑与关闭交错', project=self.project,
            reporter=self.tester, status='verified',
        )
        close_url = reverse('defects-close', args=[defect.pk])
        edit_url = reverse('defects-detail', args=[defect.pk])
        original_update = DefectWriteSerializer.update

        def finish_close_before_save(serializer, instance, validated_data):
            self.assertEqual(instance.status, 'verified')
            response = self.client.post(close_url, {}, format='json')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            return original_update(serializer, instance, validated_data)

        with patch.object(DefectWriteSerializer, 'update', finish_close_before_save):
            response = self.client.patch(edit_url, {'title': '已编辑标题'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        defect.refresh_from_db()
        self.assertEqual(defect.title, '已编辑标题')
        self.assertEqual(defect.status, 'closed')
        self.assertIsNotNone(defect.closed_at)
        retry = self.client.post(close_url, {}, format='json')
        self.assertEqual(retry.status_code, status.HTTP_200_OK)
        self.assertEqual(defect.transitions.filter(to_status='closed').count(), 1)

    def test_title_edit_preserves_concurrent_assignment_and_explicit_clear(self):
        defect = Defect.objects.create(title='保留并发指派', project=self.project, reporter=self.tester)
        serializer = DefectWriteSerializer(defect, data={'title': '仅更新标题'}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        assigned = self.client.post(
            reverse('defects-assign', args=[defect.pk]),
            {'assignee_id': self.developer.pk},
            format='json',
        )
        self.assertEqual(assigned.status_code, status.HTTP_200_OK)

        serializer.save()
        defect.refresh_from_db()
        self.assertEqual(defect.assignee_id, self.developer.pk)
        self.assertEqual(defect.status, 'assigned')
        self.assertEqual(defect.title, '仅更新标题')
        cleared = self.client.patch(reverse('defects-detail', args=[defect.pk]), {'assignee_id': None}, format='json')
        self.assertEqual(cleared.status_code, status.HTTP_200_OK)
        defect.refresh_from_db()
        self.assertIsNone(defect.assignee_id)

    def test_close_and_backfilled_history_are_retained_when_operator_is_deleted(self):
        operator = User.objects.create_user(username='retired_operator')
        ProjectMember.objects.create(project=self.project, user=operator, role='tester')
        day = timezone.localdate() - timedelta(days=1)
        closed_at = self.local_noon(day)
        regular = Defect.objects.create(
            title='关闭后重开', project=self.project,
            reporter=self.tester, status='verified',
        )
        legacy = Defect.objects.create(
            title='旧数据关闭后重开', project=self.project,
            reporter=self.tester, status='closed', closed_at=closed_at,
        )
        self.client.force_authenticate(operator)
        with patch('apps.defects.views.timezone.now', return_value=closed_at):
            close = self.client.post(reverse('defects-close', args=[regular.pk]), {}, format='json')
        self.assertEqual(close.status_code, status.HTTP_200_OK)
        for defect in (regular, legacy):
            reopen = self.client.post(reverse('defects-reopen', args=[defect.pk]), {}, format='json')
            self.assertEqual(reopen.status_code, status.HTTP_200_OK)
            self.assertIsNone(reopen.data['closed_at'])
            self.assertEqual(defect.transitions.get(to_status='closed').operator_id, operator.pk)

        self.client.force_authenticate(self.tester)
        self.assertEqual(self.closed_trend(days=2)[day.isoformat()], 2)
        operator.delete()
        self.assertEqual(self.closed_trend(days=2)[day.isoformat()], 2)

        for defect in (regular, legacy):
            detail = self.client.get(reverse('defects-detail', args=[defect.pk]))
            self.assertEqual(detail.status_code, status.HTTP_200_OK)
            closure = next(item for item in detail.data['transitions'] if item['to_status'] == 'closed')
            self.assertIsNone(closure['operator'])
            self.assertEqual(detail.data['status'], 'reopened')

    def test_summary_separates_same_named_versions_and_aggregates_a_shared_version(self):
        shared_project = Project.objects.create(name='共享项目', owner=self.tester)
        other_project = Project.objects.create(name='独立项目', owner=self.tester)
        hidden_project = Project.objects.create(name='不可见项目', owner=self.other_user)
        self.version.projects.add(shared_project, hidden_project)
        other_version = Version.objects.create(name=self.version.name, created_by=self.tester)
        other_version.projects.add(other_project)
        for project in (self.project, shared_project):
            Defect.objects.create(title='共享同一版本', project=project, reporter=self.tester, version=self.version)
        for index in range(3):
            Defect.objects.create(title=f'独立版本缺陷 {index}', project=other_project, reporter=self.tester, version=other_version)
        Defect.objects.create(title='不可见的版本缺陷', project=hidden_project, reporter=self.other_user, version=self.version)
        Defect.objects.create(title='未设置版本', project=self.project, reporter=self.tester)

        response = self.client.get(reverse('defects-summary'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        versions = {row['version_id']: row for row in response.data['version_distribution']}
        self.assertEqual({version_id: row['count'] for version_id, row in versions.items()}, {
            None: 1,
            self.version.pk: 2,
            other_version.pk: 3,
        })
        shared = versions[self.version.pk]
        separate = versions[other_version.pk]
        self.assertEqual(shared['version__name'], separate['version__name'])
        self.assertIn(self.project.name, shared['label'])
        self.assertIn(shared_project.name, shared['label'])
        self.assertNotIn(hidden_project.name, shared['label'])
        self.assertIn(other_project.name, separate['label'])
        self.assertNotEqual(shared['label'], separate['label'])

    def test_export_report(self):
        Defect.objects.create(title='阻塞缺陷', project=self.project, reporter=self.tester, severity='blocker')

        response = self.client.get(reverse('defects-export-report'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('attachment; filename="defect-report.pdf"', response['Content-Disposition'])

    def build_payload(self):
        return {
            'title': '登录失败',
            'description': '输入正确账号密码后仍提示失败',
            'reproduce_steps': '1. 打开登录页\n2. 输入账号密码\n3. 点击登录',
            'expected_result': '登录成功',
            'actual_result': '提示登录失败',
            'project_id': self.project.id,
            'version_id': self.version.id,
            'module': '登录',
            'severity': 'critical',
            'priority': 'p1',
            'defect_type': 'functional',
            'source': 'manual',
            'assignee_id': self.developer.id,
        }
