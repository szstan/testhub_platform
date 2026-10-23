from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import render
import requests
from .models import AssistantSession, AssistantMessage, ChatMessage, DifyConfig
from .views_config import _validate_dify_url
from .serializers import (
    AssistantSessionSerializer, 
    AssistantSessionCreateSerializer,
    AssistantMessageSerializer,
    ChatMessageSerializer
)


class AssistantSessionViewSet(viewsets.ModelViewSet):
    """智能助手会话视图集"""
    queryset = AssistantSession.objects.none()
    permission_classes = [permissions.IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return AssistantSessionCreateSerializer
        return AssistantSessionSerializer
    
    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return AssistantSession.objects.none()
        return AssistantSession.objects.filter(user=self.request.user)
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
    
    @action(detail=True, methods=['post'])
    def add_message(self, request, pk=None):
        """添加消息到会话"""
        session = self.get_object()
        serializer = AssistantMessageSerializer(data=request.data)
        
        if serializer.is_valid():
            serializer.save(session=session)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['get'])
    def messages(self, request, pk=None):
        """获取会话的聊天消息"""
        session = self.get_object()
        messages = session.chat_messages.all()
        serializer = ChatMessageSerializer(messages, many=True)
        return Response(serializer.data)


class ChatViewSet(viewsets.ViewSet):
    """聊天功能ViewSet"""
    permission_classes = [permissions.IsAuthenticated]
    
    @action(detail=False, methods=['post'])
    def send_message(self, request):
        """发送消息到Dify API"""
        session_id = request.data.get('session_id')
        message = request.data.get('message')
        
        if not isinstance(session_id, str) or not session_id.strip() or not isinstance(message, str) or not message.strip():
            return Response(
                {'error': 'session_id和message都是必填项'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if len(session_id) > 200:
            return Response({'error': 'session_id 长度不能超过 200'}, status=status.HTTP_400_BAD_REQUEST)
        
        session = AssistantSession.objects.filter(
            session_id=session_id,
            user=request.user
        ).order_by('-updated_at').first()
        if session is None:
            return Response(
                {'error': '会话不存在'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # 获取Dify配置
        dify_config = DifyConfig.get_active_config()
        if not dify_config:
            return Response(
                {'error': '未配置Dify API，请先在配置中心配置'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # 保存用户消息
        user_message = ChatMessage.objects.create(
            session=session,
            role='user',
            content=message,
            conversation_id=session.conversation_id
        )
        
        try:
            # 调用Dify API
            headers = {
                'Authorization': f'Bearer {dify_config.api_key}',
                'Content-Type': 'application/json'
            }
            
            payload = {
                'inputs': {},
                'query': message,
                'user': str(request.user.id),
                'response_mode': 'blocking'
            }
            
            # 如果有conversation_id，添加到请求中以保持会话连续性
            if session.conversation_id:
                payload['conversation_id'] = session.conversation_id
            
            api_url = _validate_dify_url(dify_config.api_url)
            response = requests.post(
                f'{api_url}/chat-messages',
                headers=headers,
                json=payload,
                timeout=30,
                allow_redirects=False,
            )
            
            if response.status_code == 200:
                data = response.json()
                
                # 更新会话的conversation_id
                if 'conversation_id' in data and not session.conversation_id:
                    session.conversation_id = data['conversation_id']
                    session.save()
                
                # 保存助手回复
                assistant_message = ChatMessage.objects.create(
                    session=session,
                    role='assistant',
                    content=data.get('answer', ''),
                    conversation_id=data.get('conversation_id'),
                    message_id=data.get('message_id')
                )
                
                return Response({
                    'user_message': ChatMessageSerializer(user_message).data,
                    'assistant_message': ChatMessageSerializer(assistant_message).data,
                    'conversation_id': data.get('conversation_id')
                })
            else:
                return Response({
                    'error': f'Dify API错误: {response.status_code}'
                }, status=status.HTTP_502_BAD_GATEWAY)
                
        except ValueError:
            return Response({'error': 'Dify API 地址不可用'}, status=status.HTTP_400_BAD_REQUEST)
        except requests.exceptions.Timeout:
            return Response({
                'error': 'API请求超时'
            }, status=status.HTTP_408_REQUEST_TIMEOUT)
        except requests.exceptions.RequestException:
            return Response({
                'error': 'API请求失败'
            }, status=status.HTTP_502_BAD_GATEWAY)


def assistant_view(request):
    """智能助手页面视图 - 用于iframe内嵌"""
    return render(request, 'assistant/assistant.html')
