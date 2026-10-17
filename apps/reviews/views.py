from django.db.models import Q
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.users.models import User

from .models import (
    ReviewAssignment,
    ReviewTemplate,
    TestCaseReview,
    TestCaseReviewComment,
)
from .serializers import (
    ReviewAssignmentSerializer,
    ReviewTemplateCreateSerializer,
    ReviewTemplateSerializer,
    TestCaseReviewCommentCreateSerializer,
    TestCaseReviewCommentSerializer,
    TestCaseReviewCreateSerializer,
    TestCaseReviewSerializer,
)


def get_accessible_projects(user):
    return Project.objects.filter(
        Q(owner=user) | Q(members=user)
    ).distinct()


def validate_project_scope(user, project_ids):
    project_ids = set(project_ids)
    accessible_ids = set(
        get_accessible_projects(user).values_list('id', flat=True)
    )
    if not project_ids or not project_ids.issubset(accessible_ids):
        raise PermissionDenied('没有权限访问部分项目')
    return project_ids


class TestCaseReviewViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return TestCaseReviewCreateSerializer
        return TestCaseReviewSerializer

    def get_queryset(self):
        queryset = TestCaseReview.objects.select_related('creator').prefetch_related(
            'projects', 'testcases', 'reviewers', 'comments', 'reviewassignment_set__reviewer'
        ).filter(
            projects__in=get_accessible_projects(self.request.user)
        ).distinct()

        project_id = self.request.query_params.get('project')
        status_param = self.request.query_params.get('status')
        reviewer_id = self.request.query_params.get('reviewer')

        if project_id:
            queryset = queryset.filter(projects__id=project_id)
        if status_param:
            queryset = queryset.filter(status=status_param)
        if reviewer_id:
            queryset = queryset.filter(reviewers__id=reviewer_id)

        return queryset

    def _validate_review_scope(self, serializer):
        project_ids = serializer.validated_data.get('projects')
        if project_ids is None:
            project_ids = serializer.instance.projects.values_list('id', flat=True)
        project_ids = validate_project_scope(self.request.user, project_ids)

        testcase_ids = serializer.validated_data.get('testcases')
        if testcase_ids is None:
            testcase_ids = serializer.instance.testcases.values_list('id', flat=True)
        testcase_ids = set(testcase_ids)
        if testcase_ids:
            accessible_count = TestCase.objects.filter(
                id__in=testcase_ids,
                project_id__in=project_ids,
            ).count()
            if accessible_count != len(testcase_ids):
                raise PermissionDenied('没有权限访问部分测试用例')

    def perform_create(self, serializer):
        self._validate_review_scope(serializer)
        serializer.save(creator=self.request.user)

    def perform_update(self, serializer):
        self._validate_review_scope(serializer)
        serializer.save()

    @action(detail=True, methods=['post'])
    def assign_reviewers(self, request, pk=None):
        review = self.get_object()
        reviewer_ids = request.data.get('reviewer_ids', [])

        for reviewer_id in reviewer_ids:
            try:
                reviewer = User.objects.get(id=reviewer_id)
                ReviewAssignment.objects.get_or_create(
                    review=review,
                    reviewer=reviewer,
                    defaults={'assigned_at': timezone.now()}
                )
            except User.DoesNotExist:
                continue

        return Response({'message': '评审人员分配成功'})

    @action(detail=True, methods=['post'])
    def submit_review(self, request, pk=None):
        review = self.get_object()
        try:
            assignment = ReviewAssignment.objects.get(
                review=review,
                reviewer=request.user,
            )
            assignment.status = request.data.get('status', 'approved')
            assignment.comment = request.data.get('comment', '')
            assignment.checklist_results = request.data.get('checklist_results', {})
            assignment.reviewed_at = timezone.now()
            assignment.save()

            pending_count = ReviewAssignment.objects.filter(
                review=review,
                status='pending',
            ).count()
            if pending_count == 0:
                approved_count = ReviewAssignment.objects.filter(
                    review=review,
                    status='approved',
                ).count()
                total_count = ReviewAssignment.objects.filter(review=review).count()
                review.status = 'approved' if approved_count == total_count else 'rejected'
                review.completed_at = timezone.now()
                review.save(update_fields=['status', 'completed_at', 'updated_at'])

            return Response({'message': '评审提交成功'})
        except ReviewAssignment.DoesNotExist:
            return Response(
                {'error': '您未被分配为此评审的评审人'},
                status=status.HTTP_403_FORBIDDEN,
            )

    @action(detail=False, methods=['get'])
    def my_reviews(self, request):
        reviews = self.get_queryset().filter(
            reviewers=request.user
        ).select_related('creator').prefetch_related('projects')
        serializer = self.get_serializer(reviews, many=True)
        return Response(serializer.data)


class TestCaseReviewCommentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return TestCaseReviewCommentCreateSerializer
        return TestCaseReviewCommentSerializer

    def get_queryset(self):
        queryset = TestCaseReviewComment.objects.select_related(
            'author', 'testcase', 'review'
        ).filter(
            review__projects__in=get_accessible_projects(self.request.user)
        ).distinct()
        review_id = self.request.query_params.get('review')
        testcase_id = self.request.query_params.get('testcase')

        if review_id:
            queryset = queryset.filter(review_id=review_id)
        if testcase_id:
            queryset = queryset.filter(testcase_id=testcase_id)
        return queryset

    def perform_create(self, serializer):
        review = serializer.validated_data.get('review')
        if not review or not review.projects.filter(
            id__in=get_accessible_projects(self.request.user).values('id')
        ).exists():
            raise PermissionDenied('没有权限访问该评审')
        serializer.save(author=self.request.user)


class ReviewTemplateViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return ReviewTemplateCreateSerializer
        return ReviewTemplateSerializer

    def get_queryset(self):
        project_id = self.request.query_params.get('project')
        queryset = ReviewTemplate.objects.select_related('creator').prefetch_related(
            'project', 'default_reviewers'
        ).filter(
            project__in=get_accessible_projects(self.request.user),
            is_active=True,
        ).distinct()
        if project_id:
            queryset = queryset.filter(project__id=project_id)
        return queryset

    def _validate_projects(self, serializer):
        project_ids = serializer.validated_data.get('project')
        if project_ids is None:
            project_ids = serializer.instance.project.values_list('id', flat=True)
        validate_project_scope(self.request.user, project_ids)


    def perform_create(self, serializer):
        self._validate_projects(serializer)
        serializer.save(creator=self.request.user)

    def perform_update(self, serializer):
        self._validate_projects(serializer)
        serializer.save()
