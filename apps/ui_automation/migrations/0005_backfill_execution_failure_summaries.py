from django.db import migrations
from django.db.models import Q

# Uses only the stored payload formats, independent of runtime model definitions.
from apps.ui_automation.execution_summary import build_failure_summary


BATCH_SIZE = 20


def backfill_execution_failure_summaries(apps, schema_editor):
    using = schema_editor.connection.alias
    for model_name, payload_field in (
        ('TestExecution', 'result_data'),
        ('TestCaseExecution', 'execution_logs'),
    ):
        executions = apps.get_model('ui_automation', model_name).objects.using(using)
        missing = executions.filter(
            Q(failure_summary__isnull=True) | Q(failure_summary={}),
            status__in=('FAILED', 'failed', 'ERROR', 'error'),
        )
        if model_name == 'TestExecution':
            missing = missing.filter(test_suite__isnull=False)
        else:
            missing = missing.filter(test_suite__isnull=True).exclude(execution_source__iexact='suite')

        last_pk = 0
        while True:
            batch = list(missing.filter(pk__gt=last_pk).order_by('pk').values_list(
                'pk', 'status', 'error_message', payload_field,
            )[:BATCH_SIZE])
            if not batch:
                break
            for pk, status, error, payload in batch:
                summary = build_failure_summary(status, error, payload, suite=model_name == 'TestExecution')
                missing.filter(pk=pk, status=status).update(failure_summary=summary)
            last_pk = batch[-1][0]


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('ui_automation', '0004_execution_failure_summaries')]
    operations = [migrations.RunPython(backfill_execution_failure_summaries, migrations.RunPython.noop)]
