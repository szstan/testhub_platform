# -*- coding: utf-8 -*-
"""从同目录下的 ui-component-pack.yaml 加载组件包"""
import os
import yaml
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from apps.app_automation.models import AppComponent


class Command(BaseCommand):
    help = '从 ui-component-pack.yaml 加载组件包到数据库'

    def add_arguments(self, parser):
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='更新已存在的内置组件，不删除未在包中声明的组件'
        )

    def handle(self, *args, **options):
        # YAML 文件路径（与本脚本同目录）
        current_dir = os.path.dirname(os.path.abspath(__file__))
        yaml_path = os.path.join(current_dir, 'ui-component-pack.yaml')
        
        if not os.path.exists(yaml_path):
            self.stdout.write(self.style.ERROR(f'❌ 文件不存在: {yaml_path}'))
            return
        
        self.stdout.write(f'📖 读取组件包: {yaml_path}')
        
        # 读取 YAML
        with open(yaml_path, 'r', encoding='utf-8') as f:
            manifest = yaml.safe_load(f)
        
        if not isinstance(manifest, dict) or not isinstance(manifest.get('components'), list):
            raise CommandError('component pack must be a YAML object with a components list')

        components = manifest['components']
        seen_types = set()
        for index, item in enumerate(components):
            if not isinstance(item, dict):
                raise CommandError(f'component at index {index} must be a YAML object')
            component_type = item.get('type')
            if not isinstance(component_type, str) or not component_type.strip() or component_type != component_type.strip():
                raise CommandError(f'component at index {index} must have a non-empty trimmed type')
            if component_type in seen_types:
                raise CommandError(f'duplicate component type in pack: {component_type}')
            seen_types.add(component_type)
            for key in ('schema', 'default_config'):
                if key in item and item[key] is not None and not isinstance(item[key], dict):
                    raise CommandError(f'component {component_type} {key} must be a YAML object')

        overwrite = options['overwrite']
        
        self.stdout.write(self.style.SUCCESS(f'📦 组件包信息:'))
        self.stdout.write(f'   名称: {manifest.get("name", "N/A")}')
        self.stdout.write(f'   版本: {manifest.get("version", "N/A")}')
        self.stdout.write(f'   作者: {manifest.get("author", "N/A")}')
        self.stdout.write(f'   组件数: {len(components)}')
        self.stdout.write('')
        
        created_count = 0
        updated_count = 0
        skipped_count = 0
        
        with transaction.atomic():
            for item in components:
                component_type = item['type']
                
                defaults = {
                    'name': item.get('name') or component_type,
                    'category': item.get('category', ''),
                    'description': item.get('description', ''),
                    'schema': item.get('schema') or {},
                    'default_config': item.get('default_config') or {},
                    'enabled': item.get('enabled', True),
                    'sort_order': item.get('sort_order', 0),
                }
                
                # 检查是否已存在
                existing = AppComponent.objects.filter(type=component_type).first()
                
                if existing and not overwrite:
                    skipped_count += 1
                    self.stdout.write(f'⏭️  跳过组件: {defaults["name"]} ({component_type}) - 已存在')
                    continue
                
                obj, created = AppComponent.objects.update_or_create(
                    type=component_type,
                    defaults=defaults
                )
                
                if created:
                    created_count += 1
                    self.stdout.write(self.style.SUCCESS(f'✅ 创建组件: {obj.name} ({obj.type})'))
                else:
                    updated_count += 1
                    self.stdout.write(self.style.WARNING(f'♻️  更新组件: {obj.name} ({obj.type})'))
            
        # 统计
        self.stdout.write('')
        self.stdout.write('=' * 60)
        self.stdout.write(self.style.SUCCESS('🎉 导入完成！'))
        self.stdout.write(f'   新建: {created_count} 个')
        self.stdout.write(f'   更新: {updated_count} 个')
        self.stdout.write(f'   跳过: {skipped_count} 个')
        self.stdout.write(f'   总计: {AppComponent.objects.filter(enabled=True).count()} 个启用的组件')
        
        self.stdout.write('=' * 60)
        self.stdout.write('')
        
        # 提示信息
        if skipped_count > 0 and not overwrite:
            self.stdout.write(self.style.WARNING(
                f'💡 提示: 有 {skipped_count} 个组件已存在被跳过。'
            ))
            self.stdout.write(self.style.WARNING(
                '   如需覆盖，请使用: python manage.py load_component_pack --overwrite'
            ))
            self.stdout.write('')
        
        self.stdout.write(self.style.SUCCESS('📋 下一步:'))
        self.stdout.write('   1. 启动前端: cd frontend && npm run dev')
        self.stdout.write('   2. 访问场景编排器: http://localhost:3000/app-automation/test-cases/scene-builder')
        self.stdout.write('   3. 在左侧组件面板查看所有组件')
