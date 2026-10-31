from rest_framework import serializers

from apps.users.schema import UsersSessionEmailField


class ProjectsErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()


class ProjectsAuthFailureResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class ProjectsMemberUserResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = UsersSessionEmailField(allow_blank=True)
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    avatar = serializers.CharField(allow_null=True)
    phone = serializers.CharField(allow_null=True)
    department = serializers.CharField(allow_null=True)
    position = serializers.CharField(allow_null=True)
    is_active = serializers.BooleanField()
    date_joined = serializers.DateTimeField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class ProjectsMemberCreateRequestSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(write_only=True)
    role = serializers.ChoiceField(
        choices=('owner', 'admin', 'developer', 'tester', 'viewer'),
        required=False,
        default='tester',
    )
    joined_at = serializers.DateTimeField(required=False)


class ProjectsMemberListItemResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = UsersSessionEmailField(allow_blank=True)
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    role = serializers.ChoiceField(choices=('owner', 'admin', 'developer', 'tester', 'viewer'))


class ProjectsMemberResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    user = ProjectsMemberUserResponseSerializer()
    role = serializers.ChoiceField(choices=('owner', 'admin', 'developer', 'tester', 'viewer'))
    joined_at = serializers.DateTimeField()


class ProjectsMemberValidationErrorResponseSerializer(serializers.Serializer):
    user_id = serializers.ListField(child=serializers.CharField(), required=False)
    role = serializers.ListField(child=serializers.CharField(), required=False)
    joined_at = serializers.ListField(child=serializers.CharField(), required=False)
    non_field_errors = serializers.ListField(child=serializers.CharField(), required=False)


class ProjectsGetAllItemResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    description = serializers.CharField()
    status = serializers.ChoiceField(choices=('active', 'paused', 'completed', 'archived'))


class ProjectsUserDropdownItemResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    status = serializers.ChoiceField(choices=('active', 'paused', 'completed', 'archived'))


class ProjectsUserDropdownResponseSerializer(serializers.Serializer):
    results = ProjectsUserDropdownItemResponseSerializer(many=True)


class ProjectsMessageResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
