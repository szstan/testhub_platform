from rest_framework import serializers
from django.contrib.auth import authenticate
from .models import User, UserProfile
from .registration import (
    DuplicatePhoneError,
    create_user_with_unique_phone,
    ensure_phone_available,
    serialized_phone_write,
)

class UserSimpleSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'avatar')

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name',
                 'avatar', 'phone', 'department', 'position', 'is_active',
                 'date_joined', 'created_at', 'updated_at']
        read_only_fields = ['id', 'date_joined', 'created_at', 'updated_at']

    def validate_phone(self, value):
        phone = value.strip() if isinstance(value, str) else value
        if self.instance and phone == self.instance.phone:
            return phone
        try:
            ensure_phone_available(phone, exclude_user_id=getattr(self.instance, 'pk', None))
        except DuplicatePhoneError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return phone

    def update(self, instance, validated_data):
        try:
            return _update_user(instance, validated_data)
        except DuplicatePhoneError as exc:
            raise serializers.ValidationError({'phone': str(exc)}) from exc


def _update_user(instance, validated_data):
    fields = tuple(validated_data)
    if not fields:
        return instance

    if 'phone' not in validated_data:
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save(update_fields=(*fields, 'updated_at'))
        instance.refresh_from_db()
        return instance

    phone = validated_data['phone']
    # Lock phone stripes before the user row, matching registration's lock order.
    # If the instance is stale, reject rather than locking an unexpected stripe
    # after the user row and risking a deadlock with a concurrent registration.
    with serialized_phone_write(instance.phone, phone):
        current = User.objects.select_for_update().get(pk=instance.pk)
        if current.phone != instance.phone:
            raise serializers.ValidationError({'phone': '手机号已更改，请刷新后重试'})
        if phone != current.phone:
            ensure_phone_available(
                phone,
                exclude_user_id=current.pk,
                current_read=True,
            )
        for field, value in validated_data.items():
            setattr(current, field, value)
        current.save(update_fields=(*fields, 'updated_at'))
    return current


def user_session_data(user):
    data = UserSerializer(user).data
    data['is_admin'] = bool(user.is_staff or user.is_superuser)
    return data

class UserDirectorySerializer(serializers.ModelSerializer):
    """安全的用户目录表示，不暴露手机号或管理状态。"""

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'first_name', 'last_name', 'avatar')
        read_only_fields = fields


class UserSelfUpdateSerializer(serializers.ModelSerializer):
    """允许用户修改的个人资料字段。"""

    class Meta:
        model = User
        fields = ('email', 'first_name', 'last_name', 'avatar', 'department', 'position')

    def update(self, instance, validated_data):
        return _update_user(instance, validated_data)


class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=6)
    password_confirm = serializers.CharField(write_only=True)
    
    class Meta:
        model = User
        fields = ['username', 'email', 'password', 'password_confirm',
                 'first_name', 'last_name', 'phone', 'department', 'position']
    
    def validate(self, attrs):
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError("密码不一致")
        return attrs

    def validate_phone(self, value):
        phone = value.strip() if isinstance(value, str) else value
        if phone not in (None, '') and User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError('该手机号已被注册')
        return phone

    def create(self, validated_data):
        validated_data.pop('password_confirm')
        try:
            return create_user_with_unique_phone(**validated_data)
        except DuplicatePhoneError as exc:
            raise serializers.ValidationError({'phone': str(exc)}) from exc

class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField()
    
    def validate(self, attrs):
        username = attrs.get('username')
        password = attrs.get('password')
        
        if username and password:
            user = authenticate(username=username, password=password)
            if not user:
                raise serializers.ValidationError('用户名或密码错误')
            if not user.is_active:
                raise serializers.ValidationError('用户已被禁用')
        else:
            raise serializers.ValidationError('用户名和密码不能为空')
        
        attrs['user'] = user
        return attrs

class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = '__all__'
