from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0007_workbench_async_execution'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcaseexecution',
            name='dispatch_delivery_token',
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='testcaseexecution',
            name='dispatch_delivery_expires_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='testcaseexecution',
            name='dispatch_enqueued_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='testexecution',
            name='dispatch_delivery_token',
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='testexecution',
            name='dispatch_delivery_expires_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='testexecution',
            name='dispatch_enqueued_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddIndex(
            model_name='testcaseexecution',
            index=models.Index(
                fields=['status', 'dispatch_enqueued_at', 'dispatch_delivery_expires_at'],
                name='ui_case_dispatch_recon_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='testexecution',
            index=models.Index(
                fields=['status', 'dispatch_enqueued_at', 'dispatch_delivery_expires_at'],
                name='ui_suite_dispatch_recon_idx',
            ),
        ),
    ]
