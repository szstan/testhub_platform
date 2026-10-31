"""Separate unified Project identities from numeric legacy source project IDs.

Ambiguous old profiles/quarantines remain intact in a hidden namespace. They
are never rebound to another project's policy without reliable provenance.
"""
from django.db import migrations, models


_SOURCE_TYPES = {'api': 'API', 'ui': 'UI', 'app': 'APP', 'performance': 'PERF'}


def _rebuild_profile(Profile, Item, *, alias, identity, name=''):
    profile, _created = Profile.objects.using(alias).get_or_create(**identity, defaults={'name': name})
    rows = list(Item.objects.using(alias).filter(
        run__source_type=identity['source_type'], run__project_type=identity['project_type'],
        run__project_id=identity['project_id'], test_key=identity['test_key'],
        environment_fingerprint=identity['environment_fingerprint'],
    ).order_by('-created_at', '-id').values('outcome', 'name', 'created_at')[:max(1, profile.window_size)])
    classes = [
        'PASS' if row['outcome'] == 'PASS' else 'FAIL'
        for row in reversed(rows) if row['outcome'] in {'PASS', 'FAIL', 'ERROR'}
    ]
    transitions = sum(a != b for a, b in zip(classes, classes[1:]))
    passes, failures = classes.count('PASS'), classes.count('FAIL')
    sufficient = len(classes) >= max(1, profile.min_attempts)
    flaky = sufficient and passes > 0 and failures > 0 and transitions >= 2
    profile.name = next((row['name'] for row in rows if row['name']), name or profile.name)
    profile.eligible_attempts = len(classes)
    profile.pass_count, profile.fail_count = passes, failures
    profile.transition_count = transitions
    profile.flaky_score = round(transitions / (len(classes) - 1), 6) if len(classes) > 1 else 0.0
    profile.reliability_status = 'FLAKY' if flaky else ('STABLE' if sufficient else 'INSUFFICIENT_DATA')
    times = [row['created_at'] for row in rows]
    if times:
        profile.first_seen_at = min(times)
        profile.last_seen_at = max(times)
    profile.metadata = {'namespace_repair': 'recomputed_from_explicit_domain', 'window_count': len(rows)}
    profile.save(using=alias)
    return profile


def repair_unified_namespaces(apps, schema_editor):
    alias = schema_editor.connection.alias
    Run = apps.get_model('quality_intelligence', 'QualityRun')
    Item = apps.get_model('quality_intelligence', 'QualityRunItem')
    Profile = apps.get_model('quality_intelligence', 'QualityTestProfile')
    Baseline = apps.get_model('quality_intelligence', 'QualityBaseline')
    Observation = apps.get_model('quality_intelligence', 'QualityMetricObservation')
    Comparison = apps.get_model('quality_intelligence', 'QualityComparison')
    Job = apps.get_model('executions', 'ExecutionJob')
    affected = set()
    unified = set()
    for run in Run.objects.using(alias).filter(source_execution_id__startswith='unified:').iterator(chunk_size=200):
        provenance = run.provenance if isinstance(run.provenance, dict) else {}
        suffix = run.source_execution_id.partition(':')[2]
        job_id = provenance.get('job_id')
        job = None
        if (provenance.get('source') == 'unified_execution' and suffix.isascii() and suffix.isdigit()
                and str(job_id) == suffix and len(suffix) <= 19 and 0 < int(suffix) <= 9223372036854775807):
            job = Job.objects.using(alias).filter(pk=int(suffix)).first()
        reliable = job is not None and _SOURCE_TYPES.get(job.source_type) == run.source_type
        old_domain, old_project = run.project_type, run.project_id
        identities = list(Item.objects.using(alias).filter(run_id=run.pk).values_list('test_key', 'environment_fingerprint').distinct())
        for key, env in identities:
            affected.add((run.source_type, old_domain, old_project, key, env))
        run.project_type = 'PROJECT' if reliable else 'UNRESOLVED_UNIFIED'
        if reliable:
            run.project_id = str(job.project_id)
        run.provenance = {**provenance, 'project_type': run.project_type, 'project_id': run.project_id,
                          'namespace_repair': 'verified_job_provenance' if reliable else 'unverified_preserved'}
        run.save(using=alias, update_fields=['project_type', 'project_id', 'provenance'])
        if reliable:
            for key, env in identities:
                unified.add((run.source_type, 'PROJECT', run.project_id, key, env))
    fields = ('source_type', 'project_type', 'project_id', 'test_key', 'environment_fingerprint')
    for values in unified:
        identity = dict(zip(fields, values))
        profile = _rebuild_profile(Profile, Item, alias=alias, identity=identity)
        scoped_runs = Run.objects.using(alias).filter(source_type=values[0], project_type='PROJECT', project_id=values[2])
        # References on explicit unified evidence can be repaired without touching
        # old manually authored baselines or quarantine approvals.
        Observation.objects.using(alias).filter(
            run__in=scoped_runs, item__test_key=values[3], item__environment_fingerprint=values[4],
        ).update(profile_id=profile.pk)
        Comparison.objects.using(alias).filter(
            run__in=scoped_runs, profile__test_key=values[3], profile__environment_fingerprint=values[4],
        ).update(profile_id=profile.pk)
    for values in affected:
        if values in unified:
            continue
        identity = dict(zip(fields, values))
        profiles = Profile.objects.using(alias).filter(**identity)
        if not profiles.exists():
            continue
        remaining = Item.objects.using(alias).filter(
            run__source_type=values[0], run__project_type=values[1], run__project_id=values[2],
            test_key=values[3], environment_fingerprint=values[4],
        ).exists()
        if remaining:
            _rebuild_profile(Profile, Item, alias=alias, identity=identity)
        else:
            # Retain the old PK and its quarantine/audit history, hidden from
            # ordinary domain authorization. Never transplant its approvals.
            for profile in profiles:
                profile.project_type = f'UNRESOLVED_PROFILE:{profile.pk}'[:64]
                profile.metadata = {**(profile.metadata or {}), 'namespace_repair': 'ambiguous_approval_retained'}
                profile.save(using=alias, update_fields=['project_type', 'metadata'])
                Baseline.objects.using(alias).filter(profile_id=profile.pk).update(project_type=profile.project_type)


class Migration(migrations.Migration):
    dependencies = [
        ('quality_intelligence', '0006_scale_indexes'),
        ('executions', '0018_scale_indexes'),
    ]
    operations = [
        migrations.RunPython(repair_unified_namespaces, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='qualityfailurecluster',
            index=models.Index(fields=['source_type', 'project_type', 'project_id', 'test_key', 'environment_fingerprint', 'resolved_at'], name='qi_cluster_case_env_fixed_idx'),
        ),
    ]
