from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.projects.models import Project, ProjectMember
from apps.reviews.models import (
    ReviewAssignment,
    ReviewTemplate,
    TestCaseReview,
    TestCaseReviewComment,
)
from apps.testcases.models import TestCase
from apps.users.models import User


class ReviewSecurityRegressionTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='review-owner', phone='22222222222')
        self.reviewer = User.objects.create_user(username='review-developer', phone='33333333333')
        self.viewer = User.objects.create_user(username='review-viewer')
        self.outside = User.objects.create_user(username='review-outside')
        self.project = Project.objects.create(name='Review project', owner=self.owner)
        ProjectMember.objects.create(project=self.project, user=self.reviewer, role='developer')
        ProjectMember.objects.create(project=self.project, user=self.viewer, role='viewer')
        self.testcase = self.make_testcase(self.project, 'Review test case')
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def make_testcase(self, project, title):
        return TestCase.objects.create(
            project=project,
            title=title,
            expected_result='Expected result',
            author=self.owner,
        )

    def make_review(self, project=None, testcase=None, reviewers=()):
        review = TestCaseReview.objects.create(title='Existing review', creator=self.owner)
        review.projects.set([project or self.project])
        review.testcases.set([testcase or self.testcase])
        for reviewer in reviewers:
            ReviewAssignment.objects.create(review=review, reviewer=reviewer)
        return review

    def review_payload(self, **overrides):
        payload = {
            'title': 'API review',
            'projects': [self.project.pk],
            'testcases': [self.testcase.pk],
            'reviewers': [self.reviewer.pk],
        }
        payload.update(overrides)
        return payload

    def test_review_creation_accepts_valid_relations_and_omits_private_user_fields(self):
        response = self.client.post(
            reverse('reviews-list'), self.review_payload(), format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        review = TestCaseReview.objects.get(title='API review')
        self.assertEqual(review.projects.get(), self.project)
        self.assertEqual(review.testcases.get(), self.testcase)
        self.assertEqual(review.reviewers.get(), self.reviewer)
        details = self.client.get(reverse('reviews-detail', args=[review.pk]))
        nested_reviewer = details.data['assignments'][0]['reviewer']
        self.assertIn('email', nested_reviewer)
        self.assertNotIn('phone', nested_reviewer)
        self.assertNotIn('is_staff', nested_reviewer)

    def test_template_creation_accepts_project_scoped_default_reviewers(self):
        response = self.client.post(
            reverse('review-templates-list'),
            {
                'name': 'Valid template',
                'project': [self.project.pk],
                'default_reviewers': [self.reviewer.pk],
                'checklist': ['Correctness'],
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        template = ReviewTemplate.objects.get(name='Valid template')
        self.assertEqual(list(template.project.all()), [self.project])
        self.assertEqual(list(template.default_reviewers.all()), [self.reviewer])

    def test_review_creation_rejects_duplicate_unknown_and_unreadable_reviewers(self):
        url = reverse('reviews-list')
        duplicate = self.client.post(
            url, self.review_payload(reviewers=[self.reviewer.pk, self.reviewer.pk]),
            format='json',
        )
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)

        unknown = self.client.post(
            url, self.review_payload(reviewers=[999999]), format='json',
        )
        self.assertEqual(unknown.status_code, status.HTTP_400_BAD_REQUEST)

        inaccessible = self.client.post(
            url, self.review_payload(reviewers=[self.outside.pk]), format='json',
        )
        self.assertEqual(inaccessible.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(TestCaseReview.objects.exists())

    def test_review_creation_rejects_foreign_testcases_and_templates(self):
        foreign_project = Project.objects.create(name='Foreign review project', owner=self.outside)
        foreign_testcase = self.make_testcase(foreign_project, 'Foreign case')
        foreign_template = ReviewTemplate.objects.create(name='Foreign template', creator=self.outside)
        foreign_template.project.add(foreign_project)

        foreign_case = self.client.post(
            reverse('reviews-list'), self.review_payload(testcases=[foreign_testcase.pk]),
            format='json',
        )
        self.assertEqual(foreign_case.status_code, status.HTTP_400_BAD_REQUEST)

        unknown_template = self.client.post(
            reverse('reviews-list'), self.review_payload(template=999999), format='json',
        )
        self.assertEqual(unknown_template.status_code, status.HTTP_400_BAD_REQUEST)

        foreign_template_link = self.client.post(
            reverse('reviews-list'), self.review_payload(template=foreign_template.pk),
            format='json',
        )
        self.assertEqual(foreign_template_link.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(TestCaseReview.objects.exists())

    def test_reviewer_must_read_every_project_in_a_multi_project_review(self):
        second_project = Project.objects.create(name='Second project', owner=self.owner)
        second_testcase = self.make_testcase(second_project, 'Second case')

        response = self.client.post(
            reverse('reviews-list'),
            self.review_payload(
                projects=[self.project.pk, second_project.pk],
                testcases=[self.testcase.pk, second_testcase.pk],
            ),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(TestCaseReview.objects.exists())

    def test_viewer_cannot_mutate_review_template_comment_or_assignment(self):
        review = self.make_review(reviewers=[self.reviewer])
        template = ReviewTemplate.objects.create(name='Read-only template', creator=self.owner)
        template.project.add(self.project)
        comment = TestCaseReviewComment.objects.create(
            review=review, testcase=self.testcase, author=self.owner,
            content='Existing comment',
        )
        self.client.force_authenticate(self.viewer)

        review_update = self.client.patch(
            reverse('reviews-detail', args=[review.pk]), {'title': 'Viewer edit'}, format='json',
        )
        review_create = self.client.post(
            reverse('reviews-list'), self.review_payload(), format='json',
        )
        template_create = self.client.post(
            reverse('review-templates-list'),
            {'name': 'Viewer template', 'project': [self.project.pk]}, format='json',
        )
        comment_create = self.client.post(
            reverse('review-comments-list'),
            {'review': review.pk, 'testcase': self.testcase.pk, 'content': 'Viewer comment'},
            format='json',
        )
        review_delete = self.client.delete(reverse('reviews-detail', args=[review.pk]))
        template_delete = self.client.delete(reverse('review-templates-detail', args=[template.pk]))
        comment_delete = self.client.delete(reverse('review-comments-detail', args=[comment.pk]))
        template_update = self.client.patch(
            reverse('review-templates-detail', args=[template.pk]),
            {'name': 'Viewer edit'}, format='json',
        )
        comment_update = self.client.patch(
            reverse('review-comments-detail', args=[comment.pk]),
            {'content': 'Viewer edit'}, format='json',
        )
        assign = self.client.post(
            reverse('reviews-assign-reviewers', args=[review.pk]),
            {'reviewer_ids': [self.outside.pk]}, format='json',
        )
        submit = self.client.post(
            reverse('reviews-submit-review', args=[review.pk]),
            {'status': 'approved'}, format='json',
        )

        for response in (
            review_create, template_create, comment_create, review_update,
            review_delete, template_update, template_delete, comment_update,
            comment_delete, assign, submit,
        ):
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        review.refresh_from_db()
        template.refresh_from_db()
        comment.refresh_from_db()
        self.assertEqual(review.title, 'Existing review')
        self.assertEqual(template.name, 'Read-only template')
        self.assertEqual(comment.content, 'Existing comment')
        self.assertEqual(review.reviewers.count(), 1)

    def test_review_update_rejects_foreign_testcase_and_template(self):
        review = self.make_review()
        foreign_project = Project.objects.create(name='Foreign update project', owner=self.outside)
        foreign_case = self.make_testcase(foreign_project, 'Foreign update case')
        foreign_template = ReviewTemplate.objects.create(
            name='Foreign update template', creator=self.outside,
        )
        foreign_template.project.add(foreign_project)
        url = reverse('reviews-detail', args=[review.pk])

        foreign_case_update = self.client.patch(
            url, {'testcases': [foreign_case.pk]}, format='json',
        )
        foreign_template_update = self.client.patch(
            url, {'template': foreign_template.pk}, format='json',
        )

        self.assertEqual(foreign_case_update.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(foreign_template_update.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(set(review.testcases.values_list('pk', flat=True)), {self.testcase.pk})
        review.refresh_from_db()
        self.assertIsNone(review.template_id)

    def test_review_update_requires_write_access_to_source_and_destination(self):
        mover = User.objects.create_user(username='review-mover')
        destination = Project.objects.create(name='Destination project', owner=self.owner)
        ProjectMember.objects.create(project=self.project, user=mover, role='viewer')
        ProjectMember.objects.create(project=destination, user=mover, role='developer')
        destination_case = self.make_testcase(destination, 'Destination case')
        review = self.make_review()
        self.client.force_authenticate(mover)

        response = self.client.patch(
            reverse('reviews-detail', args=[review.pk]),
            {'projects': [destination.pk], 'testcases': [destination_case.pk]},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(set(review.projects.values_list('pk', flat=True)), {self.project.pk})

    def test_template_update_requires_write_access_to_source_project(self):
        mover = User.objects.create_user(username='template-mover')
        ProjectMember.objects.create(project=self.project, user=mover, role='viewer')
        template = ReviewTemplate.objects.create(name='Existing template', creator=self.owner)
        template.project.add(self.project)
        self.client.force_authenticate(mover)

        response = self.client.patch(
            reverse('review-templates-detail', args=[template.pk]),
            {'name': 'Changed template'}, format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        template.refresh_from_db()
        self.assertEqual(template.name, 'Existing template')

    def test_template_update_requires_write_access_to_destination_project(self):
        template = ReviewTemplate.objects.create(name='Source template', creator=self.owner)
        template.project.add(self.project)
        destination = Project.objects.create(name='Unwritable destination', owner=self.outside)

        response = self.client.patch(
            reverse('review-templates-detail', args=[template.pk]),
            {'project': [destination.pk]}, format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(set(template.project.values_list('pk', flat=True)), {self.project.pk})

    def test_comment_rejects_testcase_not_in_review_and_unwritable_destination(self):
        review = self.make_review()
        unrelated_case = self.make_testcase(self.project, 'Not linked to review')
        invalid_case = self.client.post(
            reverse('review-comments-list'),
            {
                'review': review.pk,
                'testcase': unrelated_case.pk,
                'content': 'Invalid foreign key',
            },
            format='json',
        )
        self.assertEqual(invalid_case.status_code, status.HTTP_400_BAD_REQUEST)
        valid_case = self.client.post(
            reverse('review-comments-list'),
            {'review': review.pk, 'testcase': self.testcase.pk, 'content': 'Valid comment'},
            format='json',
        )
        self.assertEqual(valid_case.status_code, status.HTTP_201_CREATED, valid_case.data)

        destination = Project.objects.create(name='Comment destination', owner=self.owner)
        destination_review = self.make_review(
            project=destination,
            testcase=self.make_testcase(destination, 'Destination test case'),
        )
        comment = TestCaseReviewComment.objects.create(
            review=review, testcase=self.testcase, author=self.owner, content='Original',
        )
        mover = User.objects.create_user(username='comment-mover')
        ProjectMember.objects.create(project=self.project, user=mover, role='developer')
        ProjectMember.objects.create(project=destination, user=mover, role='viewer')
        self.client.force_authenticate(mover)

        move = self.client.patch(
            reverse('review-comments-detail', args=[comment.pk]),
            {'review': destination_review.pk}, format='json',
        )

        self.assertEqual(move.status_code, status.HTTP_403_FORBIDDEN)
        comment.refresh_from_db()
        self.assertEqual(comment.review_id, review.pk)

    def test_assignment_rejects_duplicates_and_unknown_reviewers(self):
        review = self.make_review()
        url = reverse('reviews-assign-reviewers', args=[review.pk])

        duplicate_input = self.client.post(
            url, {'reviewer_ids': [self.reviewer.pk, self.reviewer.pk]}, format='json',
        )
        unknown_input = self.client.post(
            url, {'reviewer_ids': [999999]}, format='json',
        )
        assigned = self.client.post(
            url, {'reviewer_ids': [self.reviewer.pk]}, format='json',
        )
        repeated_assignment = self.client.post(
            url, {'reviewer_ids': [self.reviewer.pk]}, format='json',
        )

        self.assertEqual(duplicate_input.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(unknown_input.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(assigned.status_code, status.HTTP_200_OK)
        self.assertEqual(repeated_assignment.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(review.reviewers.count(), 1)

    def test_assignment_status_is_choice_based_and_aggregate_updates_atomically(self):
        review = self.make_review(reviewers=[self.reviewer])
        self.client.force_authenticate(self.reviewer)
        url = reverse('reviews-submit-review', args=[review.pk])

        invalid = self.client.post(url, {'status': 'pending'}, format='json')
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        assignment = ReviewAssignment.objects.get(review=review, reviewer=self.reviewer)
        with patch('apps.reviews.views.TestCaseReview.save', side_effect=RuntimeError('aggregate failed')):
            with self.assertRaises(RuntimeError):
                self.client.post(url, {'status': 'approved'}, format='json')

        assignment.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(assignment.status, 'pending')
        self.assertEqual(review.status, 'pending')
        self.assertIsNone(review.completed_at)

        approved = self.client.post(url, {'status': 'approved'}, format='json')
        self.assertEqual(approved.status_code, status.HTTP_200_OK)
        assignment.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(assignment.status, 'approved')
        self.assertEqual(review.status, 'approved')
        self.assertIsNotNone(review.completed_at)

        repeated = self.client.post(url, {'status': 'rejected'}, format='json')
        self.assertEqual(repeated.status_code, status.HTTP_400_BAD_REQUEST)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, 'approved')
        new_reviewer = User.objects.create_user(username='completed-review-addition')
        ProjectMember.objects.create(project=self.project, user=new_reviewer, role='developer')
        completed_assignment = self.client.post(
            reverse('reviews-assign-reviewers', args=[review.pk]),
            {'reviewer_ids': [new_reviewer.pk]}, format='json',
        )
        self.assertEqual(completed_assignment.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(review.reviewers.count(), 1)

    def test_review_update_preserves_existing_assignment_state(self):
        review = self.make_review(reviewers=[self.reviewer])
        existing = ReviewAssignment.objects.get(review=review, reviewer=self.reviewer)
        existing.status = 'approved'
        existing.save(update_fields=['status'])
        additional = User.objects.create_user(username='additional-reviewer')
        ProjectMember.objects.create(project=self.project, user=additional, role='developer')

        response = self.client.patch(
            reverse('reviews-detail', args=[review.pk]),
            {'title': 'Updated review', 'reviewers': [self.reviewer.pk, additional.pk]},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        existing.refresh_from_db()
        self.assertEqual(existing.status, 'approved')
        self.assertEqual(
            ReviewAssignment.objects.get(review=review, reviewer=additional).status,
            'pending',
        )

    def test_assignment_creation_rolls_back_as_a_unit(self):
        response = None
        url = reverse('reviews-list')
        with patch('apps.reviews.serializers.ReviewAssignment.objects.bulk_create', side_effect=RuntimeError('assignment write failed')):
            with self.assertRaises(RuntimeError):
                response = self.client.post(url, self.review_payload(), format='json')

        self.assertIsNone(response)
        self.assertFalse(TestCaseReview.objects.exists())

    def test_read_only_viewer_cannot_be_assigned_an_unsubmittable_review(self):
        response = self.client.post(
            reverse('reviews-list'), self.review_payload(reviewers=[self.viewer.pk]), format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(TestCaseReview.objects.exists())

    def test_review_deleted_before_locked_lookup_returns_not_found_without_mutation(self):
        review = self.make_review(reviewers=[self.reviewer])
        assignment = ReviewAssignment.objects.get(review=review, reviewer=self.reviewer)
        # The initial scoped read succeeds; the locked re-fetch models a committed delete.
        locked_queryset = TestCaseReview.objects.none()

        with patch(
            'apps.reviews.views.TestCaseReview.objects.select_for_update',
            return_value=locked_queryset,
        ):
            response = self.client.post(
                reverse('reviews-submit-review', args=[review.pk]),
                {'status': 'approved'}, format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        review.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(review.status, 'pending')
        self.assertIsNone(review.completed_at)
        self.assertEqual(assignment.status, 'pending')
        self.assertIsNone(assignment.reviewed_at)

    def test_cancelled_review_cannot_be_resurrected_by_pending_assignment(self):
        review = self.make_review(reviewers=[self.reviewer])
        review.status = 'cancelled'
        review.save(update_fields=['status'])
        self.client.force_authenticate(self.reviewer)
        response = self.client.post(
            reverse('reviews-submit-review', args=[review.pk]), {'status': 'approved'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        review.refresh_from_db()
        self.assertEqual(review.status, 'cancelled')
        self.assertEqual(review.reviewassignment_set.get().status, 'pending')

    def test_same_reviewer_patch_preserves_cancelled_review_state(self):
        review = self.make_review(reviewers=[self.reviewer])
        completed_at = timezone.now()
        review.status = 'cancelled'
        review.completed_at = completed_at
        review.save(update_fields=['status', 'completed_at'])

        response = self.client.patch(
            reverse('reviews-detail', args=[review.pk]),
            {'reviewers': [self.reviewer.pk]}, format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        review.refresh_from_db()
        self.assertEqual(review.status, 'cancelled')
        self.assertEqual(review.completed_at, completed_at)
        assignment = ReviewAssignment.objects.get(review=review, reviewer=self.reviewer)
        self.assertEqual(assignment.status, 'pending')

        self.client.force_authenticate(self.reviewer)
        submit = self.client.post(
            reverse('reviews-submit-review', args=[review.pk]),
            {'status': 'approved'}, format='json',
        )
        self.assertEqual(submit.status_code, status.HTTP_400_BAD_REQUEST)

        additional = User.objects.create_user(username='cancelled-review-addition')
        ProjectMember.objects.create(project=self.project, user=additional, role='developer')
        self.client.force_authenticate(self.owner)
        assign = self.client.post(
            reverse('reviews-assign-reviewers', args=[review.pk]),
            {'reviewer_ids': [additional.pk]}, format='json',
        )
        self.assertEqual(assign.status_code, status.HTTP_400_BAD_REQUEST)

        review.refresh_from_db()
        self.assertEqual(review.status, 'cancelled')
        self.assertEqual(review.completed_at, completed_at)
        self.assertEqual(review.reviewassignment_set.count(), 1)
        self.assertEqual(review.reviewassignment_set.get().status, 'pending')

    def test_removing_last_pending_assignment_recomputes_aggregate(self):
        review = self.make_review(reviewers=[self.reviewer, self.owner])
        review.reviewassignment_set.filter(reviewer=self.reviewer).update(status='approved')
        review.status = 'in_progress'
        review.save(update_fields=['status'])
        response = self.client.patch(
            reverse('reviews-detail', args=[review.pk]), {'reviewers': [self.reviewer.pk]}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        review.refresh_from_db()
        self.assertEqual(review.status, 'approved')
        self.assertIsNotNone(review.completed_at)
        self.assertEqual(review.reviewassignment_set.count(), 1)
