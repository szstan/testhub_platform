from django.conf import settings
from django.db import transaction
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser
from .models import DifyConfig
from .serializers import DifyConfigSerializer
import requests


from urllib.parse import urlparse
import ipaddress
import socket


def _validate_dify_url(value):
    parsed = urlparse((value or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Dify API URL 必须是无凭证的 http/https 地址')
    allowed = {item.strip().lower() for item in (getattr(settings, 'DIFY_ALLOWED_HOSTS', '') or '').split(',') if item.strip()}
    hostname = parsed.hostname.lower()
    if allowed and hostname not in allowed:
        raise ValueError('Dify API 主机不在允许列表中')
    if not allowed:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise ValueError('Dify API 主机无法解析') from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                raise ValueError('Dify API URL 不允许访问内网或保留地址')
    return parsed.geturl().rstrip('/')


class DifyConfigViewSet(viewsets.ModelViewSet):
    """Dify配置管理ViewSet"""
    queryset = DifyConfig.objects.all()
    serializer_class = DifyConfigSerializer
    permission_classes = [IsAdminUser]
    
    def list(self, request):
        """获取激活的配置"""
        active_config = DifyConfig.get_active_config()
        if active_config:
            serializer = self.get_serializer(active_config)
            # 返回时隐藏完整的API key，只显示部分
            data = serializer.data
            if 'api_key' in data and data['api_key']:
                data['api_key_masked'] = data['api_key'][:8] + '****'
                del data['api_key']
            return Response(data)
        return Response({'message': '未找到激活的配置'}, status=status.HTTP_404_NOT_FOUND)
    
    def create(self, request):
        """创建新配置"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            _validate_dify_url(serializer.validated_data['api_url'])
        except ValueError as exc:
            return Response({'api_url': [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            if serializer.validated_data.get('is_active', True):
                DifyConfig.objects.update(is_active=False)
            serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    def update(self, request, pk=None, partial=False):
        """更新配置"""
        instance = self.get_object()
        
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        try:
            _validate_dify_url(serializer.validated_data.get('api_url', instance.api_url))
        except ValueError as exc:
            return Response({'api_url': [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            if serializer.validated_data.get('is_active', False):
                DifyConfig.objects.exclude(pk=pk).update(is_active=False)
            serializer.save()
        return Response(serializer.data)
    
    def partial_update(self, request, pk=None):
        """部分更新配置"""
        return self.update(request, pk=pk, partial=True)
    
    @action(detail=False, methods=['post'])
    def test_connection(self, request):
        """测试Dify API连接"""
        api_url = request.data.get('api_url')
        api_key = request.data.get('api_key')
        
        if not api_url or not api_key:
            return Response(
                {'error': 'API URL和API Key都是必填项'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            api_url = _validate_dify_url(api_url)
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            }
            
            # 使用一个简单的测试消息
            test_data = {
                'inputs': {},
                'query': 'test',
                'user': 'test_user'
            }
            
            # 去除URL末尾的斜杠
            api_url = api_url.rstrip('/')
            
            response = requests.post(
                f'{api_url}/chat-messages',
                headers=headers,
                json=test_data,
                timeout=10,
                allow_redirects=False,
            )
            
            if response.status_code == 200:
                return Response({'message': '连接成功！', 'success': True})
            else:
                return Response({
                    'error': f'连接失败: {response.status_code}',
                    'success': False
                }, status=status.HTTP_400_BAD_REQUEST)
                
        except ValueError as e:
            return Response({'error': str(e), 'success': False}, status=status.HTTP_400_BAD_REQUEST)
        except requests.exceptions.Timeout:
            return Response({
                'error': '连接超时，请检查API URL是否正确',
                'success': False
            }, status=status.HTTP_408_REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            return Response({
                'error': f'连接错误: {str(e)}',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
