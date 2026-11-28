from rest_framework import serializers
from .models import DataFactoryRecord
from .tool_list import get_tool_list
from .tools.random_tools import MAX_RANDOM_LENGTH, validate_generation_budget, _strftime_output_length


class DataFactoryRecordSerializer(serializers.ModelSerializer):
    """数据工厂记录序列化器"""
    user_name = serializers.CharField(source='user.username', read_only=True)
    tool_category_display = serializers.CharField(source='get_tool_category_display', read_only=True)
    tool_scenario_display = serializers.CharField(source='get_tool_scenario_display', read_only=True)
    tool_name_display = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = DataFactoryRecord
        fields = [
            'id', 'user', 'user_name', 'tool_name', 'tool_name_display', 'tool_category', 'tool_category_display',
            'tool_scenario', 'tool_scenario_display', 'input_data', 'output_data',
            'is_saved', 'tags', 'created_at'
        ]
        read_only_fields = ['id', 'user', 'created_at']

    def get_tool_name_display(self, obj):
        """获取工具名称的显示名称"""
        try:
            # 直接在方法内获取工具列表
            tool_list = get_tool_list()
            for tool in tool_list:
                if tool['name'] == obj.tool_name:
                    return tool['display_name']
            return obj.tool_name
        except Exception as e:
            return obj.tool_name


class ToolExecuteSerializer(serializers.Serializer):
    """工具执行序列化器"""
    tool_name = serializers.CharField(required=True)
    tool_category = serializers.CharField(required=True)
    tool_scenario = serializers.CharField(required=True)
    input_data = serializers.JSONField(required=True)
    is_saved = serializers.BooleanField(default=True)
    tags = serializers.ListField(child=serializers.CharField(), required=False, allow_null=True)

    def validate_input_data(self, value):
        if not isinstance(value, (dict, str)):
            raise serializers.ValidationError('工具输入必须是对象或字符串')
        if isinstance(value, dict) and 'count' in value:
            try:
                validate_generation_budget(value['count'])
            except ValueError as exc:
                raise serializers.ValidationError(str(exc))
        return value

    def validate(self, attrs):
        value = attrs['input_data']
        if attrs['tool_category'] != 'random' or not isinstance(value, dict):
            return attrs

        length = value.get('length')
        if 'length' in value and length is None:
            raise serializers.ValidationError({'input_data': '长度必须是有效的整数'})

        tool_name = attrs['tool_name']
        if tool_name == 'random_float':
            precision = value.get('precision', 2)
            if (
                isinstance(precision, bool)
                or not isinstance(precision, int)
                or not -MAX_RANDOM_LENGTH <= precision <= MAX_RANDOM_LENGTH
            ):
                raise serializers.ValidationError({'input_data': '精度超出允许范围'})
            length = max(1, abs(precision))
        elif tool_name == 'random_mac_address':
            separator = value.get('separator', ':')
            if not isinstance(separator, str):
                raise serializers.ValidationError({'input_data': '分隔符必须是字符串'})
            length = 12 + 5 * len(separator)
        elif tool_name == 'random_date':
            try:
                length = _strftime_output_length(value.get('date_format', '%Y-%m-%d'))
            except ValueError as exc:
                raise serializers.ValidationError({'input_data': str(exc)})

        try:
            validate_generation_budget(value.get('count', 1), length)
        except ValueError as exc:
            raise serializers.ValidationError({'input_data': str(exc)})
        return attrs
