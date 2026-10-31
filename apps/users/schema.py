from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


@extend_schema_field({
    'oneOf': [
        {'type': 'string', 'format': 'email'},
        {'type': 'string', 'enum': ['']},
    ],
})
class UsersSessionEmailField(serializers.EmailField):
    """Session responses also include users without a configured email."""


class UsersSessionResponseSerializer(serializers.Serializer):
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
    is_admin = serializers.BooleanField()


class UsersErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()


class UsersAuthFailureResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class UsersSuccessErrorResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    error = serializers.CharField()


class UsersLoginRequestSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)


class UsersLoginResponseSerializer(serializers.Serializer):
    user = UsersSessionResponseSerializer()
    access = serializers.CharField()
    message = serializers.CharField()


class UsersLoginValidationErrorResponseSerializer(serializers.Serializer):
    username = serializers.ListField(child=serializers.CharField(), required=False)
    password = serializers.ListField(child=serializers.CharField(), required=False)
    non_field_errors = serializers.ListField(child=serializers.CharField(), required=False)


class UsersRefreshRequestSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=False, allow_blank=True, write_only=True)


class UsersRefreshResponseSerializer(serializers.Serializer):
    access = serializers.CharField(read_only=True)


class UsersLogoutRequestSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=False, allow_blank=True, write_only=True)


class UsersMessageResponseSerializer(serializers.Serializer):
    message = serializers.CharField()


class UsersCaptchaResponseSerializer(serializers.Serializer):
    token = serializers.CharField()
    image = serializers.CharField(help_text='Base64-encoded CAPTCHA image data URI.')


class UsersSmsCodeRequestSerializer(serializers.Serializer):
    phone = serializers.CharField()
    captcha_token = serializers.CharField(write_only=True)
    captcha_code = serializers.CharField(write_only=True)
    mode = serializers.CharField(required=False, default='register')


class UsersSmsCodeSentResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    verify_code_token = serializers.CharField()
    message = serializers.CharField()


class UsersSmsLoginRequestSerializer(serializers.Serializer):
    phone = serializers.RegexField(regex=r'^1[3-9]\d{9}$')
    verify_code = serializers.CharField(write_only=True)
    verify_code_token = serializers.CharField(write_only=True)


class UsersSmsLoginResponseSerializer(serializers.Serializer):
    user = UsersSessionResponseSerializer()
    access = serializers.CharField()
    message = serializers.CharField()


class UsersWebsocketTicketRequestSerializer(serializers.Serializer):
    channel = serializers.ChoiceField(choices=('app-automation', 'perf-testing'))
    execution_id = serializers.RegexField(regex=r'^[0-9]{1,19}$')


class UsersWebsocketTicketResponseSerializer(serializers.Serializer):
    ticket = serializers.CharField()
    expires_in_seconds = serializers.IntegerField()
    path = serializers.CharField()


class UsersExchangeTokenRequestSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=('create', 'redeem'))
    code = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
        help_text='Required when action is redeem; omitted for create.',
    )


class UsersExchangeCreatedResponseSerializer(serializers.Serializer):
    code = serializers.CharField()


class UsersExchangeRedeemedResponseSerializer(serializers.Serializer):
    access = serializers.CharField()
    user = UsersSessionResponseSerializer()
