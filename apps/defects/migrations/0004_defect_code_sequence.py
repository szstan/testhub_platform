from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('defects', '0003_preserve_transition_operator_history'),
    ]

    operations = [
        migrations.CreateModel(
            name='DefectCodeSequence',
            fields=[
                ('day', models.DateField(primary_key=True, serialize=False)),
                ('last_number', models.PositiveIntegerField(default=0)),
            ],
            options={
                'db_table': 'defect_code_sequences',
            },
        ),
    ]
