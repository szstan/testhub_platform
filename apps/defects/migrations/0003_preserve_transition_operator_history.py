from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('defects', '0002_defect_external_key_defect_external_provider_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='defecttransition',
            name='operator',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='defect_transitions',
                to=settings.AUTH_USER_MODEL,
                verbose_name='操作人',
            ),
        ),
    ]
