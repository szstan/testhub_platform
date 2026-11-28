from django.db import migrations, models


def create_admission_lock(apps, schema_editor):
    PerfExecutionAdmission = apps.get_model('perf_testing', 'PerfExecutionAdmission')
    PerfExecutionAdmission.objects.get_or_create(key='global')


class Migration(migrations.Migration):
    dependencies = [('perf_testing', '0012_perfscheduledtask_lease')]

    operations = [
        migrations.CreateModel(
            name='PerfExecutionAdmission',
            fields=[
                ('key', models.CharField(db_comment='全局压测准入锁键', default='global', editable=False,
                                         max_length=40, primary_key=True, serialize=False)),
                ('generation', models.PositiveBigIntegerField(db_comment='SQLite 写锁版本', default=0)),
            ],
            options={
                'db_table': 'perf_execution_admission',
                'db_table_comment': '压测执行准入锁',
                'verbose_name': '压测执行准入锁',
                'verbose_name_plural': '压测执行准入锁',
            },
        ),
        migrations.RunPython(create_admission_lock, migrations.RunPython.noop),
    ]
