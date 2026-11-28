from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.users.models import User
from apps.users.serializers import UserDirectorySerializer

from .models import (
    ReviewAssignment,
    ReviewTemplate,
    TestCaseReview,
    TestCaseReviewComment,
)


class SimpleProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ('id', 'name', 'description')


class SimpleTestCaseSerializer(serializers.ModelSerializer):
    author = UserDirectorySerializer(read_only=True)

    class Meta:
        model = TestCase
        fields = ('id', 'title', 'test_type', 'priority', 'status', 'author')


class ReviewAssignmentSerializer(serializers.ModelSerializer):
    reviewer = UserDirectorySerializer(read_only=True)

    class Meta:
        model = ReviewAssignment
        fields = ['id', 'reviewer', 'status', 'comment', 'checklist_results', 'reviewed_at', 'assigned_at']


class TestCaseReviewCommentSerializer(serializers.ModelSerializer):
    author = UserDirectorySerializer(read_only=True)
    testcase = SimpleTestCaseSerializer(read_only=True)

    class Meta:
        model = TestCaseReviewComment
        fields = [
            'id', 'testcase', 'author', 'comment_type', 'content', 'step_number',
            'is_resolved', 'created_at', 'updated_at',
        ]


class TestCaseReviewCommentCreateSerializer(serializers.ModelSerializer):
    review = serializers.PrimaryKeyRelatedField(queryset=TestCaseReview.objects.all())
    testcase = serializers.PrimaryKeyRelatedField(
        queryset=TestCase.objects.all(), required=False, allow_null=True,
    )

    class Meta:
        model = TestCaseReviewComment
        fields = ['review', 'testcase', 'comment_type', 'content', 'step_number']


class ReviewTemplateSerializer(serializers.ModelSerializer):
    creator = UserDirectorySerializer(read_only=True)
    project = SimpleProjectSerializer(many=True, read_only=True)
    default_reviewers = UserDirectorySerializer(many=True, read_only=True)

    class Meta:
        model = ReviewTemplate
        fields = [
            'id', 'name', 'description', 'project', 'creator', 'checklist',
            'default_reviewers', 'is_active', 'created_at', 'updated_at',
        ]


def _reject_duplicate_ids(attrs, fields):
    for field in fields:
        values = attrs.get(field)
        if values is None:
            continue
        ids = [value.pk for value in values]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError({field: '不能包含重复对象'})


class TestCaseReviewSerializer(serializers.ModelSerializer):
    creator = UserDirectorySerializer(read_only=True)
    projects = SimpleProjectSerializer(many=True, read_only=True)
    testcases = SimpleTestCaseSerializer(many=True, read_only=True)
    template = ReviewTemplateSerializer(read_only=True)
    assignments = ReviewAssignmentSerializer(source='reviewassignment_set', many=True, read_only=True)
    comments = TestCaseReviewCommentSerializer(many=True, read_only=True)

    class Meta:
        model = TestCaseReview
        fields = [
            'id', 'title', 'description', 'projects', 'testcases', 'creator',
            'template', 'status', 'priority', 'deadline', 'created_at', 'updated_at',
            'completed_at', 'assignments', 'comments',
        ]


class TestCaseReviewCreateSerializer(serializers.ModelSerializer):
    testcases = serializers.PrimaryKeyRelatedField(
        queryset=TestCase.objects.all(), many=True, write_only=True,
    )
    reviewers = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), many=True, write_only=True,
    )
    projects = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.all(), many=True, allow_empty=False, write_only=True,
    )
    template = serializers.PrimaryKeyRelatedField(
        queryset=ReviewTemplate.objects.all(), write_only=True, required=False, allow_null=True,
    )

    class Meta:
        model = TestCaseReview
        fields = [
            'title', 'description', 'projects', 'priority', 'deadline',
            'testcases', 'reviewers', 'template',
        ]

    def validate(self, attrs):
        _reject_duplicate_ids(attrs, ('projects', 'testcases', 'reviewers'))
        if self.instance and 'reviewers' in attrs:
            desired = {reviewer.pk for reviewer in attrs['reviewers']}
            current = set(self.instance.reviewers.values_list('pk', flat=True))
            if desired != current and self.instance.status in ('approved', 'rejected', 'cancelled'):
                raise serializers.ValidationError({
                    'reviewers': '已完成或已取消的评审不能修改评审人员',
                })
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        testcases = validated_data.pop('testcases')
        reviewers = validated_data.pop('reviewers')
        projects = validated_data.pop('projects')
        template = validated_data.pop('template', None)

        review = TestCaseReview.objects.create(template=template, **validated_data)
        review.projects.set(projects)
        review.testcases.set(testcases)
        ReviewAssignment.objects.bulk_create([
            ReviewAssignment(review=review, reviewer=reviewer)
            for reviewer in reviewers
        ])
        return review

    @transaction.atomic
    def update(self, instance, validated_data):
        testcases = validated_data.pop('testcases', None)
        reviewers = validated_data.pop('reviewers', None)
        projects = validated_data.pop('projects', None)
        template = validated_data.pop('template', serializers.empty)

        for field, value in validated_data.items():
            setattr(instance, field, value)
        if validated_data:
            instance.save()
        if projects is not None:
            instance.projects.set(projects)
        if testcases is not None:
            instance.testcases.set(testcases)
        if template is not serializers.empty:
            instance.template = template
            instance.save(update_fields=['template', 'updated_at'])
        if reviewers is not None and {
            reviewer.pk for reviewer in reviewers
        } != set(instance.reviewers.values_list('pk', flat=True)):
            desired_ids = {reviewer.pk for reviewer in reviewers}
            instance.reviewassignment_set.exclude(reviewer_id__in=desired_ids).delete()
            existing_ids = set(instance.reviewers.values_list('pk', flat=True))
            ReviewAssignment.objects.bulk_create([
                ReviewAssignment(review=instance, reviewer=reviewer)
                for reviewer in reviewers if reviewer.pk not in existing_ids
            ])
            assignments = instance.reviewassignment_set.all()
            if assignments.filter(status='pending').exists():
                instance.status = 'in_progress' if assignments.exclude(status='pending').exists() else 'pending'
                instance.completed_at = None
            elif assignments.exists():
                instance.status = 'rejected' if assignments.exclude(status='approved').exists() else 'approved'
                instance.completed_at = timezone.now()
            else:
                instance.status = 'pending'
                instance.completed_at = None
            instance.save(update_fields=['status', 'completed_at', 'updated_at'])
        return instance


class ReviewTemplateCreateSerializer(serializers.ModelSerializer):
    default_reviewers = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), many=True, required=False, write_only=True,
    )
    project = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.all(), many=True, allow_empty=False, write_only=True,
    )

    class Meta:
        model = ReviewTemplate
        fields = [
            'name', 'description', 'project', 'checklist', 'default_reviewers', 'is_active',
        ]

    def validate(self, attrs):
        _reject_duplicate_ids(attrs, ('project', 'default_reviewers'))
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        default_reviewers = validated_data.pop('default_reviewers', [])
        projects = validated_data.pop('project')
        template = ReviewTemplate.objects.create(**validated_data)
        template.project.set(projects)
        template.default_reviewers.set(default_reviewers)
        return template

    @transaction.atomic
    def update(self, instance, validated_data):
        default_reviewers = validated_data.pop('default_reviewers', None)
        projects = validated_data.pop('project', None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if validated_data:
            instance.save()
        if projects is not None:
            instance.project.set(projects)
        if default_reviewers is not None:
            instance.default_reviewers.set(default_reviewers)
        return instance


class ReviewersAssignmentSerializer(serializers.Serializer):
    reviewer_ids = serializers.ListField(
        child=serializers.PrimaryKeyRelatedField(queryset=User.objects.all()),
        allow_empty=False,
    )

    def validate_reviewer_ids(self, reviewers):
        ids = [reviewer.pk for reviewer in reviewers]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError('不能重复分配同一评审人')
        return reviewers


class ReviewSubmissionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=('approved', 'rejected', 'abstained'), required=False, default='approved',
    )
    comment = serializers.CharField(required=False, allow_blank=True, default='')
    checklist_results = serializers.JSONField(required=False, default=dict)

    def validate_checklist_results(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('检查清单结果必须为对象')
        return value
