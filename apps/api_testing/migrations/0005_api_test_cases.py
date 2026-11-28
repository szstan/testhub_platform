import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('api_testing', '0004_scheduled_task_lease'),
        ('testcases', '0004_testcasesyncprofile_testcasesourcebinding_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ApiTestCase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True, default='')),
                ('preconditions', models.TextField(blank=True, default='')),
                ('expected_result', models.TextField(blank=True, default='')),
                ('priority', models.CharField(choices=[('low', '低'), ('medium', '中'), ('high', '高'), ('critical', '紧急')], default='medium', max_length=20)),
                ('status', models.CharField(choices=[('draft', '草稿'), ('active', '激活'), ('deprecated', '废弃')], default='draft', max_length=20)),
                ('tags', models.JSONField(default=list)),
                ('variables', models.JSONField(default=dict)),
                ('stop_on_failure', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_api_test_cases', to=settings.AUTH_USER_MODEL)),
                ('environment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='api_test_cases', to='api_testing.environment')),
                ('linked_testcase', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='api_test_cases', to='testcases.testcase')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='test_cases', to='api_testing.apiproject')),
            ],
            options={
                'db_table': 'api_test_cases',
                'ordering': ['-updated_at', '-pk'],
            },
        ),
        migrations.AddIndex(
            model_name='apitestcase',
            index=models.Index(fields=['project', 'status', 'priority'], name='api_case_proj_state_idx'),
        ),
        migrations.CreateModel(
            name='ApiCaseExecution',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('test_case_name', models.CharField(max_length=200)),
                ('status', models.CharField(choices=[('PENDING', '待执行'), ('RUNNING', '执行中'), ('COMPLETED', '已完成'), ('FAILED', '失败')], default='PENDING', max_length=20)),
                ('start_time', models.DateTimeField(blank=True, null=True)),
                ('end_time', models.DateTimeField(blank=True, null=True)),
                ('duration', models.FloatField(blank=True, null=True)),
                ('total_steps', models.PositiveIntegerField(default=0)),
                ('passed_steps', models.PositiveIntegerField(default=0)),
                ('failed_steps', models.PositiveIntegerField(default=0)),
                ('skipped_steps', models.PositiveIntegerField(default=0)),
                ('results', models.JSONField(default=list)),
                ('error_message', models.TextField(blank=True, default='')),
                ('idempotency_key', models.CharField(blank=True, default=None, max_length=128, null=True)),
                ('idempotency_payload_hash', models.CharField(blank=True, default='', max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('environment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='api_testing.environment')),
                ('executed_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='api_case_executions', to=settings.AUTH_USER_MODEL)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='case_executions', to='api_testing.apiproject')),
                ('test_case', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='executions', to='api_testing.apitestcase')),
            ],
            options={
                'db_table': 'api_case_executions',
                'ordering': ['-created_at', '-pk'],
            },
        ),
        migrations.AddIndex(
            model_name='apicaseexecution',
            index=models.Index(fields=['project', 'created_at'], name='api_case_exec_proj_dt_idx'),
        ),
        migrations.AddConstraint(
            model_name='apicaseexecution',
            constraint=models.UniqueConstraint(fields=('project', 'executed_by', 'idempotency_key'), name='api_case_exec_idempotency_uniq'),
        ),
        migrations.CreateModel(
            name='ApiTestCaseStep',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
                ('order', models.PositiveIntegerField(default=0)),
                ('enabled', models.BooleanField(default=True)),
                ('request_overrides', models.JSONField(blank=True, default=dict)),
                ('assertions', models.JSONField(blank=True, null=True)),
                ('extractors', models.JSONField(blank=True, default=list)),
                ('request', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='case_steps', to='api_testing.apirequest')),
                ('test_case', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='steps', to='api_testing.apitestcase')),
            ],
            options={
                'db_table': 'api_test_case_steps',
                'ordering': ['order', 'pk'],
            },
        ),
        migrations.AddConstraint(
            model_name='apitestcasestep',
            constraint=models.UniqueConstraint(fields=('test_case', 'order'), name='api_case_step_order_uniq'),
        ),
        migrations.CreateModel(
            name='TestSuiteCase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order', models.IntegerField(default=0)),
                ('enabled', models.BooleanField(default=True)),
                ('test_case', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='suite_memberships', to='api_testing.apitestcase')),
                ('test_suite', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='suite_cases', to='api_testing.testsuite')),
            ],
            options={
                'db_table': 'api_test_suite_cases',
                'ordering': ['order', 'pk'],
            },
        ),
        migrations.AddConstraint(
            model_name='testsuitecase',
            constraint=models.UniqueConstraint(fields=('test_suite', 'test_case'), name='api_suite_case_unique'),
        ),
        migrations.AddField(
            model_name='testsuite',
            name='test_cases',
            field=models.ManyToManyField(related_name='test_suites', through='api_testing.TestSuiteCase', to='api_testing.apitestcase', verbose_name='包含接口用例'),
        ),
    ]
