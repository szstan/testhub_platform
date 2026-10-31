from rest_framework import serializers

from .serializers import ChatMessageSerializer


class AssistantChatAuthErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class AssistantChatSendMessageRequestSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=200)
    message = serializers.CharField()


class AssistantChatSendMessageResponseSerializer(serializers.Serializer):
    user_message = ChatMessageSerializer()
    assistant_message = ChatMessageSerializer()
    conversation_id = serializers.CharField(allow_null=True)


class AssistantChatErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()
