import pytest

from scripts.staging_release import validate_probe_targets


@pytest.fixture
def candidate_nginx(tmp_path):
    repo = tmp_path / 'candidate'
    (repo / 'frontend' / 'dist').mkdir(parents=True)
    config = tmp_path / 'candidate-nginx.conf'
    config.write_text(
        f'''events {{ worker_connections 64; }}
http {{
    server {{
        listen 28003;
        server_name localhost;
        root "{repo / 'frontend' / 'dist'}";
        index index.html;
        location / {{ try_files $uri $uri/ /index.html; }}
        location /api/ {{ proxy_pass http://127.0.0.1:28002; }}
    }}
}}
''',
        encoding='utf-8',
    )
    return repo, config


def test_probe_targets_match_candidate_listener_and_api_upstream(candidate_nginx):
    repo, config = candidate_nginx
    assert validate_probe_targets(
        repo,
        config,
        'http://127.0.0.1:28003',
        'http://127.0.0.1:28002',
    ) == (('http', '*', 28003), ('http', '127.0.0.1', 28002))


def test_rejects_canonical_frontend_probe_for_candidate_port(candidate_nginx):
    repo, config = candidate_nginx
    with pytest.raises(ValueError, match='STAGING_FRONTEND_URL does not match'):
        validate_probe_targets(
            repo,
            config,
            'http://127.0.0.1:18003',
            'http://127.0.0.1:28002',
        )


def test_rejects_backend_probe_that_bypasses_candidate_upstream(candidate_nginx):
    repo, config = candidate_nginx
    with pytest.raises(ValueError, match='STAGING_BACKEND_URL does not match'):
        validate_probe_targets(
            repo,
            config,
            'http://localhost:28003',
            'http://127.0.0.1:18002',
        )


def test_rejects_candidate_config_rooted_at_another_checkout(candidate_nginx, tmp_path):
    repo, config = candidate_nginx
    wrong_root = tmp_path / 'other' / 'frontend' / 'dist'
    config.write_text(config.read_text(encoding='utf-8').replace(str(repo / 'frontend' / 'dist'), str(wrong_root)), encoding='utf-8')
    with pytest.raises(ValueError, match='exactly one server rooted at this candidate'):
        validate_probe_targets(
            repo,
            config,
            'http://127.0.0.1:28003',
            'http://127.0.0.1:28002',
        )


@pytest.mark.parametrize(
    'override',
    [
        'root "/old/frontend/dist";',
        'alias "/old/frontend/dist/";',
    ],
)
def test_rejects_location_level_frontend_root_override(candidate_nginx, override):
    repo, config = candidate_nginx
    source = config.read_text(encoding='utf-8')
    config.write_text(source.replace('location / {', f'location / {{ {override}'), encoding='utf-8')
    with pytest.raises(ValueError, match='override the candidate frontend root|override frontend assets'):
        validate_probe_targets(
            repo,
            config,
            'http://127.0.0.1:28003',
            'http://127.0.0.1:28002',
        )


def test_rejects_competing_frontend_proxy_location(candidate_nginx):
    repo, config = candidate_nginx
    source = config.read_text(encoding='utf-8')
    config.write_text(
        source.replace('location /api/', 'location /assets/ { proxy_pass http://old_frontend; }\n        location /api/'),
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='competing frontend proxy routing'):
        validate_probe_targets(
            repo,
            config,
            'http://127.0.0.1:28003',
            'http://127.0.0.1:28002',
        )


def test_rejects_declared_named_upstream_group(candidate_nginx):
    repo, config = candidate_nginx
    source = config.read_text(encoding='utf-8')
    source = source.replace(
        'http {',
        'http { upstream testhub_api { server 127.0.0.1:28002; }',
    ).replace(
        'proxy_pass http://127.0.0.1:28002;',
        'proxy_pass http://testhub_api;',
    )
    config.write_text(source, encoding='utf-8')
    with pytest.raises(ValueError, match='named Nginx upstream targets'):
        validate_probe_targets(
            repo,
            config,
            'http://127.0.0.1:28003',
            'http://testhub_api',
        )
