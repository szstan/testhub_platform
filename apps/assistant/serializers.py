from django.conf import settings
from rest_framework import serializers
from .models import AssistantSession, AssistantMessage, DifyConfig, ChatMessage


ASSISTANT_MESSAGE_MAX_LENGTH = getattr(settings, 'ASSISTANT_MESSAGE_MAX_LENGTH', 16000)


class DifyConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = DifyConfig
        fields = ['id', 'api_url', 'api_key', 'is_active', 'created_at', 'updated_at']
        extra_kwargs = {
            'api_key': {'write_only': True}  # Don't expose API key in responses
        }


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ['id', 'role', 'content', 'conversation_id', 'message_id', 'created_at']
        read_only_fields = ['conversation_id', 'message_id', 'created_at']


class ChatSendMessageSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=200, trim_whitespace=False)
    message = serializers.CharField(max_length=ASSISTANT_MESSAGE_MAX_LENGTH, trim_whitespace=False)

    def validate_session_id(self, value):
        if not value.strip():
            raise serializers.ValidationError('session_id is required')
        return value

    def validate_message(self, value):
        if not value.strip():
            raise serializers.ValidationError('message is required')
        return value


class AssistantMessageSerializer(serializers.ModelSerializer):
    content = serializers.CharField(max_length=ASSISTANT_MESSAGE_MAX_LENGTH)

    class Meta:
        model = AssistantMessage
        fields = ['id', 'message_type', 'content', 'created_at']


class AssistantSessionSerializer(serializers.ModelSerializer):
    messages = AssistantMessageSerializer(many=True, read_only=True)
    chat_messages = ChatMessageSerializer(many=True, read_only=True)
    
    class Meta:
        model = AssistantSession
        fields = ['id', 'session_id', 'conversation_id', 'title', 'created_at', 'updated_at', 'messages', 'chat_messages']


class AssistantSessionCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssistantSession
        fields = ['session_id', 'title']
    
    def create(self, validated_data):
        user = self.context['request'].user
        session, _created = AssistantSession.objects.get_or_create(
            user=user,
            session_id=validated_data['session_id'],
            defaults={'title': validated_data.get('title', '')},
        )
        return session