from scripts.tests.test_frontend_deploy import (
    add_previous_dist, assert_previous_dist_restored, deploy_workspace, run_build,
)


def test_health_failure_recreates_bind_mount_for_new_and_rollback_dist(deploy_workspace):
    frontend, env, bash = deploy_workspace
    add_previous_dist(frontend)
    (frontend / 'docker-compose.yml').write_text('services: {}\n', encoding='utf-8')
    bin_dir = frontend.parent / 'bin'
    docker = bin_dir / 'docker'
    docker.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$MOCK_COMPOSE_LOG"\nexit 0\n',
        encoding='utf-8',
    )
    curl = bin_dir / 'curl'
    curl.write_text('#!/usr/bin/env bash\nexit 1\n', encoding='utf-8')
    docker.chmod(0o755)
    curl.chmod(0o755)
    log_path = frontend.parent / 'compose.txt'
    env.update({
        'MOCK_COMPOSE_LOG': str(log_path), 'TESTHUB_HEALTH_ATTEMPTS': '1',
        'TESTHUB_HEALTH_INTERVAL': '0',
    })
    result = run_build(frontend, env, bash, action='start')
    assert result.returncode != 0
    assert_previous_dist_restored(frontend)
    promotions = [line for line in log_path.read_text(encoding='utf-8').splitlines() if ' up -d' in line]
    assert len(promotions) == 2
    assert all(line.endswith('up -d --force-recreate') for line in promotions)
