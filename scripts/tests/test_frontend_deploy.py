import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO_ROOT / "frontend" / "deploy.sh"


@pytest.fixture
def deploy_workspace(tmp_path):
    bash = shutil.which("bash")
    real_id = shutil.which("id")
    real_mv = shutil.which("mv")
    if not bash or not real_id or not real_mv:
        pytest.skip("frontend deploy shell tests require bash, id, and mv")

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    shutil.copy2(DEPLOY_SCRIPT, frontend / "deploy.sh")
    (frontend / "package-lock.json").write_text("{}\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \"${1:-}\" == ci ]]; then exit \"${MOCK_NPM_CI_STATUS:-0}\"; fi\n"
        "if [[ \"${1:-}\" != run || \"${2:-}\" != build ]]; then exit 90; fi\n"
        "shift 2\n"
        "out_dir=\n"
        "while (($#)); do\n"
        "  if [[ \"$1\" == --outDir ]]; then out_dir=\"$2\"; shift 2; else shift; fi\n"
        "done\n"
        "test -n \"$out_dir\"\n"
        "printf '%s\\n' \"$out_dir\" > \"$MOCK_BUILD_OUTDIR_LOG\"\n"
        "mkdir -p \"$out_dir/assets/nested\"\n"
        "printf 'new build\\n' > \"$out_dir/index.html\"\n"
        "printf 'asset\\n' > \"$out_dir/assets/nested/app.js\"\n"
        "if [[ \"${MOCK_NPM_BUILD_FAIL:-0}\" == 1 ]]; then exit 22; fi\n",
        encoding="utf-8",
    )
    restorecon = bin_dir / "restorecon"
    restorecon.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$MOCK_RESTORECON_LOG\"\n"
        "[[ \"${MOCK_RESTORECON_FAIL:-0}\" != 1 ]]\n",
        encoding="utf-8",
    )
    fake_id = bin_dir / "id"
    fake_id.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == nginx ]]; then exit 0; fi\n"
        "exec \"$REAL_ID\" \"$@\"\n",
        encoding="utf-8",
    )
    runuser = bin_dir / "runuser"
    runuser.write_text(
        "#!/usr/bin/env bash\n"
        "[[ \"${1:-}\" == -u && \"${2:-}\" == nginx && \"${3:-}\" == -- ]] || exit 90\n"
        "shift 3\n"
        "if [[ \"${MOCK_NGINX_ACCESS_FAIL:-0}\" == 1 ]]; then exit 13; fi\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    fake_mv = bin_dir / "mv"
    fake_mv.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == -- ]]; then shift; fi\n"
        "if [[ \"${MOCK_FAIL_PROMOTION:-0}\" == 1 && \"${1:-}\" == *'/.dist.staging.'* && \"${2##*/}\" == dist ]]; then exit 41; fi\n"
        "exec \"$REAL_MV\" \"$@\"\n",
        encoding="utf-8",
    )
    for command in (npm, restorecon, fake_id, runuser, fake_mv):
        command.chmod(0o755)

    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "REAL_ID": real_id,
            "REAL_MV": real_mv,
            "TMPDIR": str(temp_dir),
            "MOCK_BUILD_OUTDIR_LOG": str(tmp_path / "build-outdir.txt"),
            "MOCK_RESTORECON_LOG": str(tmp_path / "restorecon.txt"),
        }
    )
    return frontend, env, bash


def run_build(frontend, env, bash, action='build'):
    return subprocess.run(
        [bash, str(frontend / "deploy.sh"), action],
        cwd=frontend,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )


def add_previous_dist(frontend):
    dist = frontend / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("previous build\n", encoding="utf-8")
    return dist


def assert_previous_dist_restored(frontend):
    assert (frontend / "dist" / "index.html").read_text(encoding="utf-8") == "previous build\n"
    assert not list(frontend.glob(".dist.previous.*"))
    assert not list(frontend.glob(".dist.staging.*"))


def test_build_promotes_same_filesystem_public_artifacts_and_preserves_previous(deploy_workspace):
    frontend, env, bash = deploy_workspace
    add_previous_dist(frontend)

    result = run_build(frontend, env, bash)

    assert result.returncode == 0, result.stderr
    assert (frontend / "dist" / "index.html").read_text(encoding="utf-8") == "new build\n"
    previous = list(frontend.glob(".dist.previous.*"))
    assert len(previous) == 1
    assert (previous[0] / "index.html").read_text(encoding="utf-8") == "previous build\n"
    assert not list(frontend.glob(".dist.staging.*"))

    out_dir = Path((frontend.parent / "build-outdir.txt").read_text(encoding="utf-8").strip())
    assert out_dir.parent.name == "frontend"
    assert out_dir.name.startswith(".dist.staging.")
    if os.name != "nt":
        for path in (frontend / "dist", *[p for p in (frontend / "dist").rglob("*") if p.is_dir()]):
            assert stat.S_IMODE(path.stat().st_mode) == 0o755
        for path in (frontend / "dist").rglob("*"):
            if path.is_file():
                assert stat.S_IMODE(path.stat().st_mode) == 0o644

    restorecon_calls = (frontend.parent / "restorecon.txt").read_text(encoding="utf-8").splitlines()
    assert len(restorecon_calls) == 1
    restorecon_path = Path(restorecon_calls[0].split(maxsplit=1)[1])
    assert restorecon_path.name == "dist"
    assert restorecon_path.parent.name == "frontend"


def test_build_failure_keeps_previous_dist_and_cleans_staging(deploy_workspace):
    frontend, env, bash = deploy_workspace
    add_previous_dist(frontend)
    env["MOCK_NPM_BUILD_FAIL"] = "1"

    result = run_build(frontend, env, bash)

    assert result.returncode != 0
    assert_previous_dist_restored(frontend)


def test_nginx_readability_check_failure_rolls_back_dist(deploy_workspace):
    frontend, env, bash = deploy_workspace
    add_previous_dist(frontend)
    env["MOCK_RESTORECON_FAIL"] = "1"

    result = run_build(frontend, env, bash)

    assert result.returncode != 0
    assert_previous_dist_restored(frontend)


def test_promotion_rename_failure_restores_previous_dist(deploy_workspace):
    frontend, env, bash = deploy_workspace
    add_previous_dist(frontend)
    env["MOCK_FAIL_PROMOTION"] = "1"

    result = run_build(frontend, env, bash)

    assert result.returncode != 0
    assert_previous_dist_restored(frontend)


def test_dependency_install_failure_does_not_run_build_or_replace_dist(deploy_workspace):
    frontend, env, bash = deploy_workspace
    add_previous_dist(frontend)
    env["MOCK_NPM_CI_STATUS"] = "22"
    result = run_build(frontend, env, bash)
    assert result.returncode != 0
    assert_previous_dist_restored(frontend)
    assert not (frontend.parent / "build-outdir.txt").exists()


def test_failed_nginx_access_predicate_rolls_back_instead_of_find_succeeding(deploy_workspace):
    frontend, env, bash = deploy_workspace
    add_previous_dist(frontend)
    env["MOCK_NGINX_ACCESS_FAIL"] = "1"
    result = run_build(frontend, env, bash)
    assert result.returncode != 0
    assert_previous_dist_restored(frontend)


def test_failed_artifact_discovery_rolls_back_instead_of_silently_passing(deploy_workspace):
    frontend, env, bash = deploy_workspace
    add_previous_dist(frontend)
    real_find = subprocess.check_output([bash, '-c', 'command -v find'], text=True).strip()
    assert real_find
    fake_find = frontend.parent / 'bin' / 'find'
    fake_find.write_text(
        '#!/usr/bin/env bash\n'
        'for argument in "$@"; do\n'
        '  case "$argument" in\n'
        '    -print0|*"runuser -u nginx"*) exit 23 ;;\n'
        '  esac\n'
        'done\n'
        'exec "$REAL_FIND" "$@"\n',
        encoding='utf-8',
    )
    fake_find.chmod(0o755)
    env['REAL_FIND'] = real_find
    result = run_build(frontend, env, bash)
    assert result.returncode != 0
    assert_previous_dist_restored(frontend)
