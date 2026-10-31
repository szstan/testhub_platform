import base64
import copy
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import build_release_archive as builder
from scripts.release_integrity import safe_relative
from scripts.validate_release_manifest import validate_archive, validate_checkout, validate_manifest


RELEASE = 'fixture-release'


def git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo).decode('utf-8').strip()


def write_receipt(repo, **overrides):
    dist = repo / 'frontend' / 'dist'
    dist.mkdir(parents=True, exist_ok=True)
    (dist / 'assets').mkdir(exist_ok=True)
    (dist / 'index.html').write_bytes(b'<script src="/assets/app.js"></script>\n')
    (dist / 'assets' / 'app.js').write_bytes(b'console.log("fixed frontend");\n')
    sources = {}
    for row in git(repo, 'ls-tree', '-rz', '--full-tree', 'HEAD', '--', 'frontend').split('\0'):
        if row:
            metadata, path = row.split('\t', 1)
            sources[path] = metadata.split()[2]
    receipt = {
        'schema_version': 1, 'source_commit': git(repo, 'rev-parse', 'HEAD'),
        'source_tree': git(repo, 'rev-parse', 'HEAD^{tree}'), 'source_dirty': False,
        'source_files': sources,
        'files': {path.relative_to(dist).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in dist.rglob('*') if path.is_file() and path.name != 'build-receipt.json'},
        **overrides,
    }
    (dist / 'build-receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
    return receipt


@pytest.fixture
def release_repo(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'Release Test Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'config', 'core.autocrlf', 'false')
    git(repo, 'config', 'commit.gpgsign', 'false')
    git(repo, 'config', 'core.hooksPath', str(tmp_path / 'no-hooks'))
    (repo / '.gitignore').write_text('frontend/dist/\n.env\nmedia/\n', encoding='utf-8')
    (repo / 'app.py').write_bytes(b'print("fixed source")\r\n')
    (repo / 'requirements-prod.txt').write_bytes(b'demo==1.0\n')
    (repo / 'frontend' / 'src').mkdir(parents=True)
    (repo / 'frontend' / 'src' / 'main.js').write_bytes(b'console.log("source input");\n')
    (repo / '.env').write_bytes(b'fixture-only-private-configuration')
    git(repo, 'add', '.')
    git(repo, 'add', '-f', '.env')
    git(repo, 'commit', '-qm', 'fixture source')
    write_receipt(repo)
    monkeypatch.setattr(builder, 'REPO_ROOT', repo)
    return repo


def package(repo, tmp_path, name='release.tar.gz'):
    archive = tmp_path / name
    builder.build_archive(archive, RELEASE)
    return archive


def archive_payloads(path):
    with tarfile.open(path, 'r:gz') as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isreg()}


def rewrite_archive(original, destination, *, changes=None, extras=(), manifest_update=None):
    with tarfile.open(original, 'r:gz') as source, tarfile.open(destination, 'w:gz') as target:
        for member in source.getmembers():
            payload = source.extractfile(member).read()
            if changes and member.name in changes:
                payload = changes[member.name]
            if manifest_update and member.name.endswith('/release-manifest.json'):
                document = json.loads(payload)
                manifest_update(document)
                payload = json.dumps(document).encode('utf-8')
            member.size = len(payload)
            target.addfile(member, io.BytesIO(payload))
        for member, payload in extras:
            member.size = len(payload)
            target.addfile(member, io.BytesIO(payload) if member.isreg() else None)
    return destination


def test_fixed_tree_complete_inventory_and_exact_bytes(release_repo, tmp_path):
    (release_repo / 'untracked.py').write_text('not part of the selected commit', encoding='utf-8')
    archive = package(release_repo, tmp_path)
    assert validate_archive(archive, RELEASE) == (RELEASE, 1)
    payloads = archive_payloads(archive)
    prefix = f'testhub_platform-{RELEASE}/'
    manifest = json.loads(payloads[prefix + 'release-manifest.json'])
    assert manifest['schema_version'] == 2
    assert prefix + '.env' not in payloads
    assert prefix + 'untracked.py' not in payloads
    assert payloads[prefix + 'app.py'].endswith(b'\r\n')
    assert set(manifest['files']) == {path.removeprefix(prefix) for path in payloads if not path.endswith('/release-manifest.json')}
    # Transport bytes are stable too; tar metadata and gzip timestamps are fixed.
    second = package(release_repo, tmp_path, 'same-release.tar.gz')
    assert archive.read_bytes() == second.read_bytes()


def test_packaging_rejects_dirty_source_before_reading_build_artifacts(release_repo, tmp_path):
    (release_repo / 'app.py').write_text('changed after verification', encoding='utf-8')
    with pytest.raises(SystemExit, match='tracked source differs'):
        package(release_repo, tmp_path)
    assert not (tmp_path / 'release.tar.gz').exists()


@pytest.mark.parametrize('field,value', [('source_dirty', True), ('source_commit', 'b' * 40), ('source_tree', 'b' * 40)])
def test_packaging_rejects_unbound_or_stale_frontend_receipt(release_repo, tmp_path, field, value):
    write_receipt(release_repo, **{field: value})
    with pytest.raises(ValueError, match='frontend build is dirty, unbound'):
        package(release_repo, tmp_path)


def test_packaging_rejects_frontend_bytes_changed_after_build(release_repo, tmp_path):
    (release_repo / 'frontend' / 'dist' / 'assets' / 'app.js').write_bytes(b'tampered JS')
    with pytest.raises(ValueError, match='frontend output bytes'):
        package(release_repo, tmp_path)


@pytest.mark.parametrize('release', ['', '../escape', 'unsafe/name', 'unsafe\\name', 'x:y', '-option'])
def test_release_label_is_one_safe_component(release_repo, tmp_path, release):
    with pytest.raises(SystemExit, match='safe path component'):
        builder.build_archive(tmp_path / 'bad.tar.gz', release)


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'a/../escape', 'a\\b', 'C:/outside', './same', 'a//b', 'a\nb'])
def test_noncanonical_or_unsafe_paths_are_rejected(path):
    with pytest.raises(ValueError, match='unsafe|noncanonical'):
        safe_relative(path)


@pytest.mark.parametrize('payload_path', ['app.py', 'frontend/dist/assets/app.js'])
def test_all_source_and_javascript_bytes_are_verified(release_repo, tmp_path, payload_path):
    original = package(release_repo, tmp_path)
    broken = rewrite_archive(original, tmp_path / 'tampered.tar.gz', changes={f'testhub_platform-{RELEASE}/{payload_path}': b'changed bytes'})
    with pytest.raises(ValueError, match='hash mismatch'):
        validate_archive(broken, RELEASE)


def test_rehashing_modified_source_still_cannot_claim_original_commit(release_repo, tmp_path):
    original = package(release_repo, tmp_path)
    replacement = b'new source with a matching inventory hash'
    broken = rewrite_archive(
        original, tmp_path / 'false-commit.tar.gz', changes={f'testhub_platform-{RELEASE}/app.py': replacement},
        manifest_update=lambda manifest: manifest['files'].update({'app.py': hashlib.sha256(replacement).hexdigest()}),
    )
    with pytest.raises(ValueError, match='Git blob'):
        validate_archive(broken, RELEASE)


def test_commit_tree_proof_is_verified_against_its_object_id(release_repo, tmp_path):
    original = package(release_repo, tmp_path)
    broken = rewrite_archive(original, tmp_path / 'false-tree.tar.gz', manifest_update=lambda manifest: manifest.update(source_tree='b' * 40))
    with pytest.raises(ValueError, match='declared tree'):
        validate_archive(broken, RELEASE)


@pytest.mark.parametrize('kind', ['duplicate', 'symlink', 'hardlink', 'traversal', 'absolute', 'unlisted'])
def test_archive_rejects_unsafe_or_undeclared_members(release_repo, tmp_path, kind):
    original = package(release_repo, tmp_path)
    prefix = f'testhub_platform-{RELEASE}/'
    name = {'duplicate': prefix + 'app.py', 'traversal': prefix + '../escape.py', 'absolute': '/tmp/escape.py'}.get(kind, prefix + 'extra.py')
    member = tarfile.TarInfo(name)
    member.mode = 0o644
    if kind in {'symlink', 'hardlink'}:
        member.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
        member.linkname = '/outside/target'
    broken = rewrite_archive(original, tmp_path / f'{kind}.tar.gz', extras=[(member, b'new file' if member.isreg() else b'')])
    with pytest.raises(ValueError, match='duplicate|link|unsafe|unlisted'):
        validate_archive(broken, RELEASE)


def test_extracted_tree_rejects_unlisted_code_but_preserves_runtime_data(release_repo, tmp_path):
    original = package(release_repo, tmp_path)
    destination = tmp_path / 'extract'
    with tarfile.open(original, 'r:gz') as archive:
        archive.extractall(destination, filter='data')
    root = destination / f'testhub_platform-{RELEASE}'
    (root / '.env').write_bytes(b'fixture runtime only')
    (root / 'media').mkdir()
    (root / 'media' / 'recording.bin').write_bytes(b'fixture private artifact')
    previous = root / 'frontend' / '.dist.previous.fixture'
    previous.mkdir()
    (previous / 'index.html').write_bytes(b'previous frontend runtime artifact')
    assert validate_checkout(root, RELEASE) == (RELEASE, 1)
    (root / 'injected.py').write_bytes(b'not in the fixed source')
    with pytest.raises(ValueError, match='unlisted'):
        validate_checkout(root, RELEASE)
    assert (root / 'media' / 'recording.bin').read_bytes() == b'fixture private artifact'


def test_legacy_v1_requires_explicit_partial_integrity_opt_in():
    payloads = {'requirements-prod.txt': b'demo==1\n', 'frontend/dist/index.html': b'index'}
    manifest = {
        'schema_version': 1, 'release': RELEASE, 'source_commit': 'a' * 40,
        'requirements': [{'path': 'requirements-prod.txt', 'sha256': hashlib.sha256(payloads['requirements-prod.txt']).hexdigest()}],
        'files': {'frontend/dist/index.html': hashlib.sha256(payloads['frontend/dist/index.html']).hexdigest()},
    }
    with pytest.raises(ValueError, match='strict schema v2'):
        validate_manifest(manifest, payloads.__getitem__)
    assert validate_manifest(manifest, payloads.__getitem__, allow_legacy=True) == (RELEASE, 1)


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is exercised by the dedicated frontend CI job')
def test_real_frontend_receipt_plugin_binds_clean_tree_and_marks_dirty_input(release_repo, tmp_path):
    plugin_path = Path(__file__).resolve().parents[2] / 'frontend' / 'build' / 'release-receipt.mjs'
    code = '''
      import { pathToFileURL } from 'node:url';
      import { mkdirSync, writeFileSync } from 'node:fs';
      const { releaseReceipt } = await import(pathToFileURL(process.argv[1]));
      const root = process.argv[2];
      const plugin = releaseReceipt();
      const output = process.argv[4] || 'dist';
      plugin.configResolved({root:root+'/frontend',build:{outDir:output},mode:'production'});
      plugin.buildStart();
      if (process.argv[3] === 'stage-output') {
        mkdirSync(root+'/frontend/'+output+'/assets', {recursive:true});
        writeFileSync(root+'/frontend/'+output+'/index.html','staged build');
        writeFileSync(root+'/frontend/'+output+'/assets/app.js','staged JS');
      }
      if (process.argv[3] === 'change-during-build') writeFileSync(root+'/frontend/src/main.js','changed during build');
      plugin.writeBundle();
    '''
    def run(*args):
        return subprocess.run(['node', '--input-type=module', '-e', code, str(plugin_path), str(release_repo), *args], text=True, capture_output=True, timeout=20)
    result = run()
    assert result.returncode == 0, result.stderr
    receipt = json.loads((release_repo / 'frontend' / 'dist' / 'build-receipt.json').read_text(encoding='utf-8'))
    assert receipt['source_dirty'] is False
    assert validate_archive(package(release_repo, tmp_path), RELEASE) == (RELEASE, 1)
    for output in ['.dist.staging.fixture', 'custom-build-output']:
        result = run('stage-output', output)
        assert result.returncode == 0, result.stderr
        receipt = json.loads((release_repo / 'frontend' / output / 'build-receipt.json').read_text(encoding='utf-8'))
        assert receipt['source_dirty'] is False
        assert set(receipt['files']) == {'index.html', 'assets/app.js'}
        shutil.rmtree(release_repo / 'frontend' / output)
    (release_repo / 'frontend' / 'src' / 'untracked.js').write_bytes(b'untracked frontend input')
    result = run()
    assert result.returncode == 0, result.stderr
    receipt = json.loads((release_repo / 'frontend' / 'dist' / 'build-receipt.json').read_text(encoding='utf-8'))
    assert receipt['source_dirty'] is True
    result = run('change-during-build')
    assert result.returncode != 0
    assert 'Source inputs changed' in result.stderr


def test_failed_exclusive_creation_does_not_delete_a_racing_archive(release_repo, tmp_path, monkeypatch):
    output = tmp_path / 'racing.tar.gz'
    original_open = Path.open
    def racing_open(path, mode='r', *args, **kwargs):
        if path == output and mode == 'xb':
            with original_open(path, 'wb') as handle:
                handle.write(b'archive created by another process')
            raise FileExistsError('fixture concurrent creator')
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', racing_open)
    with pytest.raises(FileExistsError):
        builder.build_archive(output, RELEASE)
    assert output.read_bytes() == b'archive created by another process'


def test_manifest_rejects_private_paths_before_reading_them():
    data = b'index'
    manifest = {
        'schema_version': 1, 'release': RELEASE, 'source_commit': 'a' * 40,
        'requirements': [],
        'files': {'frontend/dist/index.html': hashlib.sha256(data).hexdigest(), '.env': 'a' * 64},
    }
    def read_file(path):
        assert path != '.env', 'private configuration must not be read'
        return data
    with pytest.raises(ValueError, match='forbidden payload'):
        validate_manifest(manifest, read_file, allow_legacy=True)


@pytest.mark.parametrize('document,expected', [
    ('{"schema_version":2,"schema_version":1}', 'duplicate key'),
    ('[]', 'invalid release JSON'),
])
def test_checkout_rejects_ambiguous_or_non_object_manifest(tmp_path, document, expected):
    (tmp_path / 'release-manifest.json').write_text(document, encoding='utf-8')
    with pytest.raises(ValueError, match=expected):
        validate_checkout(tmp_path)
