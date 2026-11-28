from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from apps.projects.access import writable_projects
from apps.projects.models import Project

from .models import (
    ReviewAssignment,
    ReviewTemplate,
    TestCaseReview,
    TestCaseReviewComment,
)
from .serializers import (
    ReviewersAssignmentSerializer,
    ReviewSubmissionSerializer,
    ReviewTemplateCreateSerializer,
    ReviewTemplateSerializer,
    TestCaseReviewCommentCreateSerializer,
    TestCaseReviewCommentSerializer,
    TestCaseReviewCreateSerializer,
    TestCaseReviewSerializer,
)


def get_accessible_projects(user):
    if user.is_staff or user.is_superuser:
        return Project.objects.all()
    return Project.objects.filter(Q(owner=user) | Q(members=user)).distinct()


def validate_project_scope(user, project_ids):
    project_ids = {getattr(project_id, 'pk', project_id) for project_id in project_ids}
    writable_ids = set(
        writable_projects(user).filter(pk__in=project_ids).values_list('pk', flat=True)
    )
    if not project_ids or writable_ids != project_ids:
        raise PermissionDenied('没有权限写入部分项目')
    return project_ids


def validate_reviewers_can_read(reviewers, project_ids, field_name):
    project_ids = set(project_ids)
    for reviewer in reviewers:
        if not reviewer.is_active:
            raise serializers.ValidationError({field_name: '不能分配给已停用用户'})
        readable_ids = set(
            get_accessible_projects(reviewer).values_list('pk', flat=True)
        )
        if not project_ids.issubset(readable_ids):
            raise serializers.ValidationError({field_name: '评审人必须能读取评审关联的全部项目'})
        writable_ids = set(writable_projects(reviewer).filter(pk__in=project_ids).values_list('pk', flat=True))
        if writable_ids != project_ids:
            raise serializers.ValidationError({field_name: '评审人必须能提交全部关联项目的评审，观察者不可分配'})


def validate_review_testcases(testcases, project_ids):
    if any(testcase.project_id not in project_ids for testcase in testcases):
        raise serializers.ValidationError({'testcases': '测试用例必须属于评审关联的项目'})


def validate_review_template(user, template, project_ids):
    if template is None:
        return
    template_project_ids = set(template.project.values_list('pk', flat=True))
    if not template_project_ids or not template_project_ids.issubset(project_ids):
        raise serializers.ValidationError({'template': '模板项目必须全部包含在评审项目中'})
    validate_project_scope(user, template_project_ids)
    validate_reviewers_can_read(
        template.default_reviewers.all(), template_project_ids, 'template',
    )


def readable_project_scopes(user):
    readable = get_accessible_projects(user)
    unreadable = Project.objects.exclude(pk__in=readable.order_by().values('pk'))
    return readable, unreadable


class TestCaseReviewViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return TestCaseReviewCreateSerializer
        return TestCaseReviewSerializer

    def get_queryset(self):
        readable, unreadable = readable_project_scopes(self.request.user)
        queryset = TestCaseReview.objects.select_related('creator', 'template').prefetch_related(
            'projects', 'testcases', 'reviewers', 'comments__testcase',
            'comments__author', 'reviewassignment_set__reviewer',
            'template__project', 'template__default_reviewers',
        ).filter(
            projects__in=readable,
        ).exclude(
            projects__in=unreadable,
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
        instance = serializer.instance
        source_ids = set(instance.projects.values_list('pk', flat=True)) if instance else set()
        target_projects = serializer.validated_data.get(
            'projects', instance.projects.all() if instance else [],
        )
        target_ids = {project.pk for project in target_projects}
        if instance:
            validate_project_scope(self.request.user, source_ids)
        validate_project_scope(self.request.user, target_ids)

        testcases = serializer.validated_data.get(
            'testcases', instance.testcases.all() if instance else [],
        )
        validate_review_testcases(testcases, target_ids)

        reviewers = serializer.validated_data.get(
            'reviewers', instance.reviewers.all() if instance else [],
        )
        validate_reviewers_can_read(reviewers, target_ids, 'reviewers')

        template = serializer.validated_data.get(
            'template', instance.template if instance else None,
        )
        validate_review_template(self.request.user, template, target_ids)

    def get_locked_review(self):
        review = self.get_object()
        try:
            return TestCaseReview.objects.select_for_update().get(pk=review.pk)
        except TestCaseReview.DoesNotExist as exc:
            raise NotFound() from exc

    def perform_create(self, serializer):
        self._validate_review_scope(serializer)
        with transaction.atomic():
            serializer.save(creator=self.request.user)

    def perform_update(self, serializer):
        self._validate_review_scope(serializer)
        serializer.save()

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        review = self.get_locked_review()
        serializer = self.get_serializer(review, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        self.perform_destroy(self.get_locked_review())
        return Response(status=204)

    def perform_destroy(self, instance):
        validate_project_scope(
            self.request.user, instance.projects.values_list('pk', flat=True),
        )
        instance.delete()

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def assign_reviewers(self, request, pk=None):
        review = self.get_locked_review()
        project_ids = validate_project_scope(
            request.user, review.projects.values_list('pk', flat=True),
        )
        if review.status in ('approved', 'rejected', 'cancelled'):
            raise serializers.ValidationError({'reviewer_ids': '已完成或已取消的评审不能继续分配'})
        serializer = ReviewersAssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reviewers = serializer.validated_data['reviewer_ids']
        validate_reviewers_can_read(reviewers, project_ids, 'reviewer_ids')

        existing_ids = set(review.reviewers.values_list('pk', flat=True))
        duplicate_ids = existing_ids.intersection(reviewer.pk for reviewer in reviewers)
        if duplicate_ids:
            raise serializers.ValidationError({'reviewer_ids': '评审人已分配'})
        try:
            with transaction.atomic():
                ReviewAssignment.objects.bulk_create([
                    ReviewAssignment(review=review, reviewer=reviewer)
                    for reviewer in reviewers
                ])
        except IntegrityError as exc:
            raise serializers.ValidationError({'reviewer_ids': '评审人已分配'}) from exc
        return Response({'message': '评审人员分配成功'})

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def submit_review(self, request, pk=None):
        review = self.get_locked_review()
        validate_project_scope(
            request.user, review.projects.values_list('pk', flat=True),
        )
        if review.status in ('approved', 'rejected', 'cancelled'):
            raise serializers.ValidationError({'status': '已结束的评审不能再次提交'})
        submission = ReviewSubmissionSerializer(data=request.data)
        submission.is_valid(raise_exception=True)
        try:
            assignment = ReviewAssignment.objects.select_for_update().get(
                review=review, reviewer=request.user,
            )
        except ReviewAssignment.DoesNotExist as exc:
            raise PermissionDenied('您未被分配为此评审的评审人') from exc
        if assignment.status != 'pending':
            raise serializers.ValidationError({'status': '该评审已提交，不能重复修改'})

        data = submission.validated_data
        assignment.status = data['status']
        assignment.comment = data['comment']
        assignment.checklist_results = data['checklist_results']
        assignment.reviewed_at = timezone.now()
        assignment.save(update_fields=[
            'status', 'comment', 'checklist_results', 'reviewed_at',
        ])

        assignments = ReviewAssignment.objects.filter(review=review)
        if assignments.filter(status='pending').exists():
            if review.status != 'in_progress':
                review.status = 'in_progress'
                review.completed_at = None
                review.save(update_fields=['status', 'completed_at', 'updated_at'])
        else:
            review.status = 'approved' if not assignments.exclude(status='approved').exists() else 'rejected'
            review.completed_at = timezone.now()
            review.save(update_fields=['status', 'completed_at', 'updated_at'])
        return Response({'message': '评审提交成功'})

    @action(detail=False, methods=['get'])
    def my_reviews(self, request):
        reviews = self.get_queryset().filter(
            reviewers=request.user,
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
        readable, unreadable = readable_project_scopes(self.request.user)
        queryset = TestCaseReviewComment.objects.select_related(
            'author', 'testcase', 'review',
        ).filter(
            review__projects__in=readable,
        ).exclude(
            review__projects__in=unreadable,
        ).distinct()
        review_id = self.request.query_params.get('review')
        testcase_id = self.request.query_params.get('testcase')
        if review_id:
            queryset = queryset.filter(review_id=review_id)
        if testcase_id:
            queryset = queryset.filter(testcase_id=testcase_id)
        return queryset

    def _validate_comment_scope(self, serializer):
        instance = serializer.instance
        source_review = instance.review if instance else None
        target_review = serializer.validated_data.get(
            'review', source_review,
        )
        if source_review:
            validate_project_scope(
                self.request.user, source_review.projects.values_list('pk', flat=True),
            )
        target_project_ids = validate_project_scope(
            self.request.user, target_review.projects.values_list('pk', flat=True),
        )
        testcase = serializer.validated_data.get(
            'testcase', instance.testcase if instance else None,
        )
        if testcase and (
            testcase.project_id not in target_project_ids
            or not target_review.testcases.filter(pk=testcase.pk).exists()
        ):
            raise serializers.ValidationError({'testcase': '测试用例必须属于该评审'})

    def perform_create(self, serializer):
        self._validate_comment_scope(serializer)
        serializer.save(author=self.request.user)

    def perform_update(self, serializer):
        self._validate_comment_scope(serializer)
        serializer.save()

    def perform_destroy(self, instance):
        validate_project_scope(
            self.request.user, instance.review.projects.values_list('pk', flat=True),
        )
        instance.delete()


class ReviewTemplateViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return ReviewTemplateCreateSerializer
        return ReviewTemplateSerializer

    def get_queryset(self):
        readable, unreadable = readable_project_scopes(self.request.user)
        queryset = ReviewTemplate.objects.select_related('creator').prefetch_related(
            'project', 'default_reviewers',
        ).filter(
            project__in=readable,
        ).exclude(
            project__in=unreadable,
        ).distinct()
        if self.action == 'list':
            queryset = queryset.filter(is_active=True)
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project__id=project_id)
        return queryset

    def _validate_projects(self, serializer):
        instance = serializer.instance
        source_ids = set(instance.project.values_list('pk', flat=True)) if instance else set()
        projects = serializer.validated_data.get(
            'project', instance.project.all() if instance else [],
        )
        target_ids = {project.pk for project in projects}
        if instance:
            validate_project_scope(self.request.user, source_ids)
        validate_project_scope(self.request.user, target_ids)
        default_reviewers = serializer.validated_data.get(
            'default_reviewers', instance.default_reviewers.all() if instance else [],
        )
        validate_reviewers_can_read(default_reviewers, target_ids, 'default_reviewers')

    def perform_create(self, serializer):
        self._validate_projects(serializer)
        serializer.save(creator=self.request.user)

    def perform_update(self, serializer):
        self._validate_projects(serializer)
        serializer.save()

    def perform_destroy(self, instance):
        validate_project_scope(
            self.request.user, instance.project.values_list('pk', flat=True),
        )
        instance.delete()
