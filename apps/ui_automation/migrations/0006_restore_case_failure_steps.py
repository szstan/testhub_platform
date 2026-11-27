from django.db import migrations
from django.db.models import Q

from apps.ui_automation.execution_summary import (
    EXECUTION_STACKTRACE_MARKERS,
    INTERNAL_CASE_EXECUTION_ERROR,
    build_failure_summary,
)


BATCH_SIZE = 100


def restore_case_failure_steps(apps, schema_editor):
    executions = apps.get_model('ui_automation', 'TestCaseExecution').objects.using(schema_editor.connection.alias)
    stacktrace = Q()
    for marker in EXECUTION_STACKTRACE_MARKERS:
        stacktrace |= Q(execution_logs__contains=marker)
    candidates = executions.filter(
        stacktrace,
        failure_summary={'message': INTERNAL_CASE_EXECUTION_ERROR, 'step_number': None},
    )

    last_pk = 0
    while True:
        batch = list(candidates.filter(pk__gt=last_pk).order_by('pk').values_list(
            'pk', 'status', 'error_message', 'execution_logs',
        )[:BATCH_SIZE])
        if not batch:
            break
        for pk, status, error_message, logs in batch:
            summary = build_failure_summary(status, error_message, logs)
            if summary.get('step_number') is not None:
                candidates.filter(
                    pk=pk, status=status, error_message=error_message, execution_logs=logs,
                ).update(failure_summary=summary)
        last_pk = batch[-1][0]


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('ui_automation', '0005_backfill_execution_failure_summaries')]
    operations = [migrations.RunPython(restore_case_failure_steps, migrations.RunPython.noop)]
