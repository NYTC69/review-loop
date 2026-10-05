"""Offline stage-A resolution: real local Git and a fake gh on PATH."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from scripts import materialize_pr as mp


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "operator"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "file").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "target.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "checkout", "-qb", "topic")
    (repo / "file").write_text("unpushed\n")
    git(repo, "commit", "-qam", "topic")
    head = git(repo, "rev-parse", "HEAD")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text("#!" + sys.executable + "\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['GH_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
data = json.loads(Path(os.environ['GH_DATA']).read_text())
key = 'pr' if args[:2] == ['pr', 'view'] else ('api' if args[0] == 'api' else args[-1])
response = data[key]
if isinstance(response, str):
    print(response, file=sys.stderr)
    sys.exit(1)
print(json.dumps(response))
''')
    fake.chmod(0o755)
    data = {"pr": {"number": 7, "url": "https://github.com/owner/target/pull/7",
                   "headRefOid": head, "baseRefOid": base, "baseRefName": "main",
                   "isCrossRepository": False},
            "nameWithOwner": {"nameWithOwner": "owner/target"},
            "url": {"url": "https://github.com/owner/target"},
            "api": {"number": 7, "html_url": "https://github.com/owner/target/pull/7",
                    "head": {"sha": head, "repo": {"full_name": "fork/target"}},
                    "base": {"sha": base, "ref": "main", "repo": {"full_name": "owner/target"}}}}
    data_path = tmp_path / "gh.json"
    log = tmp_path / "gh.calls"
    data_path.write_text(json.dumps(data))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GH_DATA", str(data_path))
    monkeypatch.setenv("GH_LOG", str(log))
    return repo, remote, base, head, data, data_path, log


def update(setup):
    setup[5].write_text(json.dumps(setup[4]))


def test_pr_number_pins_target_head_and_default_base(setup):
    repo, _, base, head, _, _, log = setup
    result = mp.resolve(repo, "7")
    assert result["head"] == {"oid": head, "source": "https://github.com/owner/target",
                              "fetch_ref": "refs/pull/7/head"}
    assert result["base"]["oid"] == base
    assert result["base"]["fetch_ref"] == "refs/heads/main"
    assert result["number"] == 7 and result["repository"] == "owner/target"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[1] == ["pr", "view", "7", "-R", "owner/target", "--json",
                        "number,url,headRefOid,baseRefName,baseRefOid,isCrossRepository"]
    assert calls[-1] == ["repo", "view", "owner/target", "--json", "url"]


def test_pr_url_and_fork_use_target_pull_ref(setup):
    setup[4]["pr"]["isCrossRepository"] = True
    update(setup)
    result = mp.resolve(setup[0], "https://github.com/owner/target/pull/7")
    assert result["is_cross_repository"] is True
    assert result["head"]["source"] == result["target_url"]
    assert result["head"]["fetch_ref"] == "refs/pull/7/head"
    assert "nameWithOwner" not in setup[6].read_text()


def test_old_gh_api_fallback_pins_both_api_shas(setup):
    setup[4]["pr"] = 'Unknown JSON field: "baseRefOid"'
    update(setup)
    result = mp.resolve(setup[0], "7", repository="owner/target")
    assert result["head"]["oid"] == setup[3]
    assert result["base"]["oid"] == setup[2]
    assert result["is_cross_repository"] is True
    assert '["api", "repos/owner/target/pulls/7"]' in setup[6].read_text()


def test_local_unpushed_ref_has_operator_source_and_remote_default_base(setup):
    repo, remote, base, head, *_ = setup
    result = mp.resolve(repo, "topic")
    assert result["head"] == {"oid": head, "source": str(repo), "fetch_ref": "refs/heads/topic"}
    assert result["base"] == {"oid": base, "source": str(remote), "fetch_ref": "refs/heads/main"}
    assert result["target_url"] == str(remote)


def test_remote_head_and_base_ignore_stale_tracking_refs(setup):
    repo, remote, base, head, *_ = setup
    git(repo, "update-ref", "refs/remotes/origin/main", head)
    result = mp.resolve(repo, "origin/main", "origin/main")
    assert result["head"] == result["base"] == {
        "oid": base, "source": str(remote), "fetch_ref": "refs/heads/main"}


def test_remote_head_uses_its_own_default_and_independent_base_source(setup, tmp_path):
    repo, remote, base, head, *_ = setup
    other = tmp_path / "other.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(other)], check=True)
    git(repo, "remote", "add", "other", str(other))
    result = mp.resolve(repo, "other/topic")
    assert result["head"]["oid"] == result["base"]["oid"] == head
    assert result["base"]["fetch_ref"] == "refs/heads/topic"
    result = mp.resolve(repo, "other/topic", "origin/main")
    assert result["head"]["source"] == str(other)
    assert result["base"]["source"] == str(remote) and result["base"]["oid"] == base
    with pytest.raises(mp.ResolutionError, match="unique remote"):
        mp.resolve(repo, "topic")


def test_no_remote_requires_explicit_base_and_accepts_commit_expression(setup):
    repo = setup[0]
    git(repo, "remote", "remove", "origin")
    with pytest.raises(mp.ResolutionError, match="--base required"):
        mp.resolve(repo, "topic")
    result = mp.resolve(repo, "HEAD", "HEAD~1")
    assert result["base"]["oid"] == setup[2]
    assert result["base"]["fetch_ref"] is None
    assert result["target_url"] == str(repo)


def test_explicit_base_overrides_pr_base_with_local_source(setup):
    result = mp.resolve(setup[0], "7", "topic")
    assert result["base"]["oid"] == setup[3]
    assert result["base"]["source"] == str(setup[0])
    assert result["target_url"] == "https://github.com/owner/target"


@pytest.mark.parametrize("field,value", [("headRefOid", "bad"), ("baseRefOid", None),
    ("baseRefName", ""), ("number", 8), ("url", "https://github.com/other/repo/pull/7"),
    ("isCrossRepository", None)])
def test_incomplete_or_mismatched_pr_refuses_without_fallback(setup, field, value):
    setup[4]["pr"][field] = value
    update(setup)
    with pytest.raises(mp.ResolutionError):
        mp.resolve(setup[0], "7")
    assert '"api"' not in setup[6].read_text()


@pytest.mark.parametrize("failure", ["not logged in", "PR not found"])
def test_gh_failure_never_tries_api_or_local_diff(setup, failure):
    setup[4]["pr"] = failure
    update(setup)
    with pytest.raises(mp.ResolutionError, match="gh resolution failed"):
        mp.resolve(setup[0], "7")
    assert '"api"' not in setup[6].read_text()


def test_api_failure_and_missing_fields_refuse(setup):
    setup[4]["pr"] = 'Unknown JSON field: "baseRefOid"'
    for response in ["not logged in", {}, {**setup[4]["api"], "head": {}}]:
        setup[4]["api"] = response
        update(setup)
        with pytest.raises(mp.ResolutionError):
            mp.resolve(setup[0], "7")


@pytest.mark.parametrize("value,base", [("missing", "main"), ("topic", "missing"),
    ("origin/missing", None), ("origin/", None),
    ("https://gitlab.com/a/b/pull/7", None), ("-x", "main")])
def test_unresolved_input_or_base_refuses(setup, value, base):
    with pytest.raises(mp.ResolutionError):
        mp.resolve(setup[0], value, base)


def test_missing_gh_and_malformed_json_refuse(setup):
    fake = Path(os.environ["PATH"].split(os.pathsep)[0]) / "gh"
    fake.write_text("#!" + sys.executable + "\nprint('not JSON')\n")
    with pytest.raises(mp.ResolutionError, match="invalid gh JSON"):
        mp.resolve(setup[0], "7")
    fake.write_text("#!" + sys.executable + "\nimport sys; sys.exit(127)\n")
    with pytest.raises(mp.ResolutionError, match="gh resolution failed"):
        mp.resolve(setup[0], "7")


def test_cli_outputs_pins_or_refusal_without_operator_mutations(setup):
    repo = setup[0]
    script = Path(mp.__file__).resolve()
    before = {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob('*') if p.is_file()}
    result = subprocess.run([sys.executable, str(script), "topic", "--repo", str(repo)],
                            text=True, capture_output=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["head"]["oid"] == setup[3]
    failed = subprocess.run([sys.executable, str(script), "missing", "--repo", str(repo)],
                            text=True, capture_output=True)
    assert failed.returncode == 2 and failed.stdout == "" and "REFUSED:" in failed.stderr
    after = {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob('*') if p.is_file()}
    assert after == before


def test_missing_gh_refuses_without_fallback(setup, monkeypatch, tmp_path):
    import shutil
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(only_git))
    with pytest.raises(mp.ResolutionError, match="gh unavailable"):
        mp.resolve(setup[0], "7")


@pytest.mark.parametrize("response", [[], None, {"url": "https://github.com/fork/target"}])
def test_invalid_target_repository_response_refuses(setup, response):
    setup[4]["url"] = response
    update(setup)
    with pytest.raises(mp.ResolutionError):
        mp.resolve(setup[0], "7")


def test_repository_conflicts_and_invalid_names_refuse(setup):
    for value, repository in [("https://github.com/owner/target/pull/7", "other/target"),
                              ("7", "-R"), ("topic", "owner/target")]:
        with pytest.raises(mp.ResolutionError):
            mp.resolve(setup[0], value, repository=repository)


def test_remote_without_default_refuses(setup):
    git(setup[1], "symbolic-ref", "HEAD", "refs/heads/missing")
    with pytest.raises(mp.ResolutionError):
        mp.resolve(setup[0], "topic")


def test_git_isolation_and_gh_only_credentials(setup, monkeypatch):
    calls = []
    original = subprocess.run
    def observe(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return original(argv, **kwargs)
    monkeypatch.setattr(mp.subprocess, "run", observe)
    monkeypatch.setenv("GIT_DIR", "/does/not/exist")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/unsafe")
    assert mp.resolve(setup[0], "origin/main")["head"]["oid"] == setup[2]
    for argv, env in calls:
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_DIR" not in env and "GIT_CONFIG_COUNT" not in env
        assert ["-c", "core.hooksPath=/dev/null"] == argv[argv.index("core.hooksPath=/dev/null")-1:][:2]
        if "ls-remote" in argv:
            assert "credential.helper=" in argv
            assert "credential.helper=!gh auth git-credential" in argv


@pytest.mark.parametrize('url', ['https://user:SECRET-TOKEN@github.com/o/r.git',
                                 'https://SECRET-TOKEN@github.com/o/r.git'])
def test_credential_remote_refuses_without_token_output_or_network(setup, monkeypatch, url):
    git(setup[0], 'remote', 'set-url', 'origin', url)
    original = subprocess.run
    def observe(argv, **kwargs):
        assert 'ls-remote' not in argv
        return original(argv, **kwargs)
    monkeypatch.setattr(mp.subprocess, 'run', observe)
    with pytest.raises(mp.ResolutionError, match='userinfo'):
        mp.resolve(setup[0], 'origin/main')
    result = original([sys.executable, mp.__file__, 'origin/main', '--repo', str(setup[0])],
                      capture_output=True, text=True)
    assert result.returncode == 2 and result.stdout == ''
    assert 'SECRET-TOKEN' not in result.stdout + result.stderr


@pytest.mark.parametrize('remote_url', ['git@offline.invalid:o/r.git', 'ssh://offline.invalid/o/r.git',
                                      'ssh://git@offline.invalid/o/r.git'])
def test_ssh_support_overrides_repo_commands_without_prompt(setup, monkeypatch, tmp_path, remote_url):
    import shutil
    from paired_session.candidate_tree import GIT_NO_EXEC
    bindir = Path(os.environ['PATH'].split(os.pathsep)[0])
    ssh_log = tmp_path / 'ssh.json'
    fake_ssh = bindir / 'ssh'
    fake_ssh.write_text('#!' + sys.executable + '\n' +
                       'import json, os, sys\nfrom pathlib import Path\n' +
                       f'Path({str(ssh_log)!r}).write_text(json.dumps(sys.argv[1:]))\n' +
                       'if "-G" in sys.argv: sys.exit(0)\n' +
                       f'os.execv({shutil.which("git")!r}, ["git", "upload-pack", {str(setup[1])!r}])\n')
    fake_ssh.chmod(0o755)
    sentinel = tmp_path / 'unsafe-ran'
    git(setup[0], 'config', 'core.sshCommand', 'touch ' + str(sentinel))
    git(setup[0], 'remote', 'set-url', 'origin', remote_url)
    calls = []
    original = subprocess.run
    def observe(argv, **kwargs):
        calls.append((argv, kwargs))
        return original(argv, **kwargs)
    monkeypatch.setattr(mp.subprocess, 'run', observe)
    result = mp.resolve(setup[0], 'origin/main')
    assert result['head']['oid'] == setup[2]
    assert result['head']['source'] == remote_url
    assert not sentinel.exists()
    assert json.loads(ssh_log.read_text())[:2] == ['-o', 'BatchMode=yes']
    for argv, kwargs in calls:
        assert argv[1:1 + len(GIT_NO_EXEC)] == list(GIT_NO_EXEC)
        assert kwargs['env']['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes'
        assert kwargs['env']['GIT_OPTIONAL_LOCKS'] == '0'
        assert kwargs['stdin'] == subprocess.DEVNULL


def test_branch_tag_ambiguity_refuses_and_full_ref_remains_explicit(setup):
    git(setup[0], 'tag', 'topic', setup[2])
    with pytest.raises(mp.ResolutionError, match='ambiguous'):
        mp.resolve(setup[0], 'topic')
    assert mp.resolve(setup[0], 'refs/heads/topic')['head']['oid'] == setup[3]
    assert mp.resolve(setup[0], 'refs/tags/topic')['head']['oid'] == setup[2]


@pytest.mark.parametrize('kind', ['branch', 'tag'])
def test_local_remote_name_collision_refuses(setup, kind):
    git(setup[0], kind, 'origin/main', setup[3])
    with pytest.raises(mp.ResolutionError, match='ambiguous local and remote'):
        mp.resolve(setup[0], 'origin/main')


@pytest.mark.parametrize('ref', ['refs/remotes/origin/main', 'remotes/origin/main'])
def test_tracking_ref_prefix_refuses_stale_local_resolution(setup, ref):
    git(setup[0], 'update-ref', 'refs/remotes/origin/main', setup[3])
    with pytest.raises(mp.ResolutionError, match='tracking ref'):
        mp.resolve(setup[0], ref)


@pytest.mark.parametrize('ref', ['HEAD~1', 'OID'])
def test_unnamed_commit_marks_null_fetch_ref_for_d2(setup, ref):
    result = mp.resolve(setup[0], setup[2] if ref == 'OID' else ref, 'main')
    assert result['head']['oid'] == setup[2]
    assert 'fetch_ref' in result['head'] and result['head']['fetch_ref'] is None
    assert json.loads(json.dumps(result))['head']['fetch_ref'] is None
    assert result['base']['fetch_ref'] == 'refs/heads/main'


def test_relative_remote_pins_absolute_source(setup):
    git(setup[0], 'remote', 'set-url', 'origin', '../target.git')
    result = mp.resolve(setup[0], 'origin/main')
    assert result['head']['source'] == result['base']['source'] == str(setup[1])
    assert result['target_url'] == str(setup[1])


@pytest.mark.parametrize('fallback', [False, True])
@pytest.mark.parametrize('requested', ['owner/target', 'old/renamed'])
def test_pr_uses_gh_canonical_identity_after_case_change_or_rename(setup, fallback, requested):
    canonical = 'Owner/Target'
    setup[4]['url']['url'] = 'https://github.com/' + canonical
    setup[4]['pr']['url'] = 'https://github.com/' + canonical + '/pull/7'
    setup[4]['api']['html_url'] = setup[4]['pr']['url']
    setup[4]['api']['base']['repo']['full_name'] = canonical
    if fallback:
        setup[4]['pr'] = 'Unknown JSON field: "baseRefOid"'
    update(setup)
    result = mp.resolve(setup[0], 'https://github.com/' + requested + '/pull/7',
                        repository=requested.upper())
    assert result['repository'] == canonical
    assert result['url'] == 'https://github.com/' + canonical + '/pull/7'
    assert result['head']['source'] == result['target_url'] == setup[4]['url']['url']


def test_deleted_fork_api_head_still_pins_pull_ref(setup):
    setup[4]['pr'] = 'Unknown JSON field: "baseRefOid"'
    setup[4]['api']['head']['repo'] = None
    update(setup)
    result = mp.resolve(setup[0], '7')
    assert result['is_cross_repository'] is True
    assert result['head']['fetch_ref'] == 'refs/pull/7/head'
    assert result['head']['oid'] == setup[3]


def test_gh_environment_ignores_git_overrides_and_preserves_auth_home(setup, monkeypatch):
    calls = []
    original = subprocess.run
    def observe(argv, **kwargs):
        calls.append((argv, kwargs))
        return original(argv, **kwargs)
    monkeypatch.setattr(mp.subprocess, 'run', observe)
    monkeypatch.setenv('GIT_DIR', '/wrong/repo.git')
    monkeypatch.setenv('GIT_WORK_TREE', '/wrong/worktree')
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    assert mp.resolve(setup[0], '7')['number'] == 7
    gh_calls = [kwargs for argv, kwargs in calls if argv[0] == 'gh']
    assert gh_calls
    for kwargs in gh_calls:
        assert not any(key.startswith('GIT_') for key in kwargs['env'])
        assert kwargs['env']['HOME'] == os.environ['HOME']
        assert kwargs['stdin'] == subprocess.DEVNULL


@pytest.mark.parametrize('identity', [None, 7, 'invalid'])
def test_deleted_fork_still_requires_valid_api_target_identity(setup, identity):
    setup[4]['pr'] = 'Unknown JSON field: "baseRefOid"'
    setup[4]['api']['head']['repo'] = None
    setup[4]['api']['base']['repo']['full_name'] = identity
    update(setup)
    with pytest.raises(mp.ResolutionError, match='API target repository'):
        mp.resolve(setup[0], '7')


@pytest.mark.parametrize('ref', ['topic', 'topic~1', 'topic^{commit}'])
def test_ambiguity_enumeration_ignores_warning_config_and_locale(setup, monkeypatch, ref):
    git(setup[0], 'tag', 'topic', setup[2])
    git(setup[0], 'config', 'core.warnAmbiguousRefs', 'false')
    monkeypatch.setenv('LC_ALL', 'de_DE')
    with pytest.raises(mp.ResolutionError, match='ambiguous ref'):
        mp.resolve(setup[0], ref, 'main')


@pytest.mark.parametrize('ref', ['main@{u}', 'main@{upstream}', 'main@{push}',
                                'main@{u}~0', 'main@{push}^{commit}', 'origin', 'origin~0'])
def test_resolved_tracking_refs_and_expressions_refuse(setup, ref):
    repo = setup[0]
    git(repo, 'update-ref', 'refs/remotes/origin/main', setup[3])
    git(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
    git(repo, 'config', 'branch.main.remote', 'origin')
    git(repo, 'config', 'branch.main.merge', 'refs/heads/main')
    git(repo, 'config', 'push.default', 'upstream')
    with pytest.raises(mp.ResolutionError, match='tracking ref'):
        mp.resolve(repo, ref, 'main')
    # The same refusal must apply independently to an explicit base.
    with pytest.raises(mp.ResolutionError, match='tracking ref'):
        mp.resolve(repo, 'topic', ref)


@pytest.mark.parametrize('url', ['ssh://git:SECRET-TOKEN@offline.invalid/o/r.git',
                                 'https://git:SECRET-TOKEN@offline.invalid/o/r.git'])
def test_password_remote_refuses_before_network(setup, monkeypatch, url):
    git(setup[0], 'remote', 'set-url', 'origin', url)
    original = subprocess.run
    def observe(argv, **kwargs):
        assert 'ls-remote' not in argv
        return original(argv, **kwargs)
    monkeypatch.setattr(mp.subprocess, 'run', observe)
    with pytest.raises(mp.ResolutionError, match='userinfo'):
        mp.resolve(setup[0], 'origin/main')


@pytest.mark.parametrize('value,base', [('7', None), ('topic', None), ('origin/main', None),
                                      ('HEAD~1', 'main'), ('topic', 'HEAD@{0}')])
def test_every_pin_has_one_uniform_shape(setup, value, base):
    result = mp.resolve(setup[0], value, base)
    for pin in (result['head'], result['base']):
        assert set(pin) == {'oid', 'source', 'fetch_ref'}
        assert pin['fetch_ref'] is None or pin['fetch_ref'].startswith('refs/')
    if value == 'HEAD~1':
        assert result['head']['fetch_ref'] is None
    if base == 'HEAD@{0}':
        assert result['base']['fetch_ref'] is None


def test_gh_repository_overrides_removed_but_credentials_retained(setup, monkeypatch):
    monkeypatch.setenv('GH_REPO', 'unrelated/repository')
    monkeypatch.setenv('GH_HOST', 'unrelated.invalid')
    monkeypatch.setenv('GH_TOKEN', 'FAKE-AUTH-TOKEN')
    original = subprocess.run
    calls = []
    def observe(argv, **kwargs):
        if argv[0] == 'gh':
            calls.append(argv)
            assert 'GH_REPO' not in kwargs['env'] and 'GH_HOST' not in kwargs['env']
            assert kwargs['env']['GH_TOKEN'] == 'FAKE-AUTH-TOKEN'
            assert kwargs['env']['HOME'] == os.environ['HOME']
        return original(argv, **kwargs)
    monkeypatch.setattr(mp.subprocess, 'run', observe)
    assert mp.resolve(setup[0], '7')['repository'] == 'owner/target'
    assert calls[0] == ['gh', 'repo', 'view', '--json', 'nameWithOwner']


def test_askpass_cannot_run_when_gh_has_no_credentials(setup, monkeypatch, tmp_path):
    import shutil
    bindir = Path(os.environ['PATH'].split(os.pathsep)[0])
    actual_git = shutil.which('git')
    sentinel = tmp_path / 'askpass-ran'
    askpass = tmp_path / 'askpass'
    askpass.write_text('#!' + sys.executable + '\nfrom pathlib import Path\n' +
                       f'Path({str(sentinel)!r}).touch()\nprint("credential")\n')
    askpass.chmod(0o755)
    git(setup[0], 'config', 'core.askPass', str(askpass))
    monkeypatch.setenv('SSH_ASKPASS', str(askpass))
    # Replace the transport with real Git credential acquisition, without a server.
    # It uses the exact production config/env; only the credential request is supplied.
    fake_git = bindir / 'git'
    fake_git.write_text('#!' + sys.executable + '\nimport os, subprocess, sys\n' +
                       'args = sys.argv[1:]\n' +
                       'if "ls-remote" not in args:\n' +
                       f'    os.execv({actual_git!r}, ["git", *args])\n' +
                       'args = args[:args.index("ls-remote")] + ["credential", "fill"]\n' +
                       f'p = subprocess.run([{actual_git!r}, *args], text=True, capture_output=True,\n' +
                       '                   input="protocol=https\\nhost=offline.invalid\\n\\n")\n' +
                       'sys.stdout.write(p.stdout); sys.stderr.write(p.stderr); sys.exit(p.returncode)\n')
    fake_git.chmod(0o755)
    original = subprocess.run
    def observe(argv, **kwargs):
        if argv[0] == 'git':
            assert 'core.askPass=' in argv
            assert 'SSH_ASKPASS' not in kwargs['env']
            assert kwargs['stdin'] == subprocess.DEVNULL
        return original(argv, **kwargs)
    monkeypatch.setattr(mp.subprocess, 'run', observe)
    with pytest.raises(mp.ResolutionError, match='git resolution failed'):
        mp.resolve(setup[0], 'origin/main')
    assert not sentinel.exists()


def test_object_name_and_ref_collision_refuses(setup):
    git(setup[0], 'tag', setup[3], setup[2])
    git(setup[0], 'config', 'core.warnAmbiguousRefs', 'false')
    with pytest.raises(mp.ResolutionError, match='ambiguous object and ref'):
        mp.resolve(setup[0], setup[3], 'main')


@pytest.mark.parametrize('namespace', ['refs/topic', 'refs/remotes/topic', 'refs/remotes/topic/HEAD'])
def test_all_dwim_namespaces_are_counted_before_resolution(setup, namespace):
    git(setup[0], 'update-ref', namespace, setup[2])
    git(setup[0], 'config', 'core.warnAmbiguousRefs', 'false')
    with pytest.raises(mp.ResolutionError, match='ambiguous ref'):
        mp.resolve(setup[0], 'topic', 'main')


def test_pseudoref_and_tag_collision_refuses_without_warnings(setup):
    git(setup[0], 'update-ref', 'ORIG_HEAD', setup[3])
    git(setup[0], 'tag', 'ORIG_HEAD', setup[2])
    git(setup[0], 'config', 'core.warnAmbiguousRefs', 'false')
    with pytest.raises(mp.ResolutionError, match='ambiguous ref'):
        mp.resolve(setup[0], 'ORIG_HEAD', 'main')


def test_history_search_expression_pins_oid_without_named_ref(setup):
    result = mp.resolve(setup[0], ':/base', 'main')
    assert result['head'] == {'oid': setup[2], 'source': str(setup[0]), 'fetch_ref': None}


def test_detached_head_and_pseudoref_use_null_fetch_ref(setup):
    git(setup[0], 'checkout', '-q', '--detach', 'topic')
    git(setup[0], 'update-ref', 'ORIG_HEAD', setup[2])
    result = mp.resolve(setup[0], 'HEAD', 'ORIG_HEAD')
    assert result['head'] == {'oid': setup[3], 'source': str(setup[0]), 'fetch_ref': None}
    assert result['base'] == {'oid': setup[2], 'source': str(setup[0]), 'fetch_ref': None}


def test_symbolic_local_ref_to_tracking_ref_refuses(setup):
    git(setup[0], 'update-ref', 'refs/remotes/origin/main', setup[3])
    git(setup[0], 'symbolic-ref', 'refs/heads/alias', 'refs/remotes/origin/main')
    with pytest.raises(mp.ResolutionError, match='tracking ref'):
        mp.resolve(setup[0], 'alias', 'main')


def test_root_pseudoref_of_any_name_counts_for_ambiguity(setup):   # N2
    repo = setup[0]
    (repo / '.git' / 'FOO_HEAD').write_text(setup[3] + '\n')
    git(repo, 'tag', 'FOO_HEAD', setup[2])
    with pytest.raises(mp.ResolutionError, match='ambiguous ref'):
        mp.resolve(repo, 'FOO_HEAD', 'main')


# --- N1: repository config cannot make git run a command through a transport ---------------------------------------
@pytest.mark.parametrize('path', ['ext', 'insteadOf', 'gitProxy'])
def test_transport_config_cannot_run_a_command(setup, tmp_path, path):
    repo = setup[0]
    sentinel = tmp_path / 'transport-ran'
    script = tmp_path / 'proxy.sh'
    script.write_text(f'#!/bin/sh\ntouch {sentinel}\n')
    script.chmod(0o755)
    ext = f'ext::sh -c touch% {sentinel}'
    if path == 'ext':
        git(repo, 'config', 'protocol.ext.allow', 'always')
        url = ext
    elif path == 'insteadOf':
        git(repo, 'config', 'protocol.ext.allow', 'always')
        git(repo, 'config', f'url.{ext} #.insteadOf', 'https://offline.invalid/')
        url = 'https://offline.invalid/o/r.git'
    else:
        git(repo, 'config', 'core.gitProxy', str(script))
        url = 'git://offline.invalid/o/r.git'
    git(repo, 'remote', 'set-url', 'origin', url)
    with pytest.raises(mp.ResolutionError):
        mp.resolve(repo, 'origin/main')
    # the up-front check aside, the environment alone keeps git from running it
    with pytest.raises(mp.ResolutionError):
        mp.command(['git', 'ls-remote', '--', url], repo, network=True)
    assert not sentinel.exists()


@pytest.mark.parametrize('url', ['ext::sh -c true', 'fd::17', 'git://offline.invalid/r', 'http://offline.invalid/r'])
def test_transport_outside_the_allowlist_is_refused_up_front(url):
    with pytest.raises(mp.ResolutionError):
        mp.check_transport(url)
    assert mp.check_transport('git@offline.invalid:o/r.git') and mp.check_transport('/abs/repo.git')


# --- LG2-d2: materialization, all local (no network) ----------------------------------------------------------------
GITHUB = 'https://github.com/owner/target'


def localize(result, target):
    """The fake gh pins GitHub URLs; point them at the local bare target, as the real target would serve them."""
    for pin in (result['head'], result['base']):
        if pin['source'] == GITHUB:
            pin['source'] = str(target)
    if result['target_url'] == GITHUB:
        result['target_url'] = str(target)
    return result


def commit(repo, name, text, message):
    (repo / name).write_text(text)
    git(repo, 'add', name)
    git(repo, 'commit', '-qm', message)
    return git(repo, 'rev-parse', 'HEAD')


def pr(setup, number, oid, cross=False):
    setup[4]['pr'].update(number=number, url=f'{GITHUB}/pull/{number}', headRefOid=oid, isCrossRepository=cross)
    update(setup)
    return localize(mp.resolve(setup[0], str(number)), setup[1])


def operator_state(repo):
    files = {str(p.relative_to(repo / '.git')): p.read_bytes() for p in (repo / '.git').rglob('*')
             if p.is_file() and p.name != 'index'}
    return (git(repo, 'for-each-ref'), git(repo, 'worktree', 'list', '--porcelain'),
            git(repo, 'status', '--porcelain'), files)


def assert_materialized(out, root, head, base):
    ws = Path(out['workspace'])
    assert ws.parent == (Path(root) / 'pr').resolve() and re.fullmatch(r'[0-9a-f]{32}', ws.name)
    assert git(ws, 'rev-parse', 'HEAD') == head
    assert subprocess.run(['git', '-C', str(ws), 'symbolic-ref', '-q', 'HEAD']).returncode != 0   # detached
    assert git(ws, 'rev-parse', 'refs/review/head') == head and git(ws, 'rev-parse', 'refs/review/base') == base
    assert git(ws, 'status', '--porcelain') == ''
    assert not (ws / '.git' / 'objects' / 'info' / 'alternates').exists()
    assert (ws / '.git' / mp.MARKER).read_text().strip() == ws.name
    return ws


def test_d2_pr_head_from_the_target_pull_ref(setup, tmp_path):
    repo, target, base, head, *_ = setup
    git(repo, 'push', '-q', str(target), 'topic:refs/pull/7/head')
    before = operator_state(repo)
    out = mp.materialize(pr(setup, 7, head), tmp_path / 'runs')
    ws = assert_materialized(out, tmp_path / 'runs', head, base)
    assert out['merge_base'] == base
    assert git(ws, 'remote', 'get-url', 'origin') == str(target)
    assert operator_state(repo) == before   # refs, config, worktree list, objects and status unchanged


def test_d2_fork_pr_head_is_served_by_the_target(setup, tmp_path):
    repo, target, base, *_ = setup
    fork = tmp_path / 'fork.git'
    subprocess.run(['git', 'clone', '-q', '--bare', str(target), str(fork)], check=True)
    work = tmp_path / 'fork-work'
    subprocess.run(['git', 'clone', '-q', str(fork), str(work)], check=True)
    git(work, 'config', 'user.email', 'f@example.invalid')
    git(work, 'config', 'user.name', 'F')
    oid = commit(work, 'fork.txt', 'fork\n', 'fork change')
    git(work, 'push', '-q', 'origin', 'HEAD:refs/heads/feature')
    git(fork, 'push', '-q', str(target), 'feature:refs/pull/8/head')   # as GitHub publishes a fork head
    out = mp.materialize(pr(setup, 8, oid, cross=True), tmp_path / 'runs')
    assert out['is_cross_repository'] is True
    assert_materialized(out, tmp_path / 'runs', oid, base)


def test_d2_remote_branch_and_unpushed_local_branch(setup, tmp_path):
    repo, target, base, head, *_ = setup
    other = tmp_path / 'other.git'
    subprocess.run(['git', 'clone', '-q', '--bare', str(repo), str(other)], check=True)
    git(repo, 'remote', 'add', 'other', str(other))
    out = mp.materialize(mp.resolve(repo, 'other/topic', 'origin/main'), tmp_path / 'runs')
    assert_materialized(out, tmp_path / 'runs', head, base)
    before = operator_state(repo)
    out = mp.materialize(mp.resolve(repo, 'topic', 'origin/main'), tmp_path / 'runs')   # unpushed: only here
    assert out['head']['source'] == str(repo) and out['base']['source'] == str(target)
    assert_materialized(out, tmp_path / 'runs', head, base)
    assert operator_state(repo) == before


def test_d2_no_remote_clones_the_operator_repo_without_alternates(setup, tmp_path):
    repo, _, base, head, *_ = setup
    git(repo, 'remote', 'remove', 'origin')
    with pytest.raises(mp.ResolutionError, match='--base required'):
        mp.resolve(repo, 'topic')
    out = mp.materialize(mp.resolve(repo, 'topic~0', 'main'), tmp_path / 'runs')   # unnamed head: fetched by OID
    assert out['head']['fetch_ref'] is None and out['target_url'] == str(repo)
    assert_materialized(out, tmp_path / 'runs', head, base)


def test_d2_moved_head_is_refused_and_the_clone_removed(setup, tmp_path, monkeypatch):
    repo, target, base, head, *_ = setup
    git(repo, 'push', '-q', str(target), 'topic:refs/pull/7/head')
    stale = pr(setup, 7, head)
    git(repo, 'push', '-q', '-f', str(target), 'main:refs/pull/7/head')   # the PR moved after resolution
    with pytest.raises(mp.MaterializeError, match='head moved'):
        mp.materialize(stale, tmp_path / 'runs')
    assert list((tmp_path / 'runs' / 'pr').iterdir()) == []
    fresh = json.loads(json.dumps(stale))
    fresh['head']['oid'] = base
    calls = iter([stale, fresh])
    monkeypatch.setattr(mp, 'resolve', lambda *args: next(calls))
    out = mp.resolve_and_materialize(repo, '7', None, None, tmp_path / 'runs')   # re-resolved once
    assert git(Path(out['workspace']), 'rev-parse', 'HEAD') == base
    calls = iter([stale, stale])
    with pytest.raises(mp.MaterializeError, match='head moved'):   # then refused
        mp.resolve_and_materialize(repo, '7', None, None, tmp_path / 'runs')


def test_d2_criss_cross_history_is_refused(setup, tmp_path):
    repo = setup[0]
    git(repo, 'checkout', '-q', '-b', 'left', 'main')
    a1 = commit(repo, 'l', 'l\n', 'left')
    git(repo, 'checkout', '-q', '-b', 'right', 'main')
    b1 = commit(repo, 'r', 'r\n', 'right')
    git(repo, 'merge', '-q', '--no-ff', '-m', 'r<-l', a1)
    git(repo, 'checkout', '-q', 'left')
    git(repo, 'merge', '-q', '--no-ff', '-m', 'l<-r', b1)
    assert len(git(repo, 'merge-base', '--all', 'left', 'right').splitlines()) == 2
    with pytest.raises(mp.MaterializeError, match='criss-cross'):
        mp.materialize(mp.resolve(repo, 'left', 'right'), tmp_path / 'runs')
    assert list((tmp_path / 'runs' / 'pr').iterdir()) == []


def test_d2_clone_survives_a_gc_of_the_operator_repo(setup, tmp_path):
    repo, _, base, head, *_ = setup
    out = mp.materialize(mp.resolve(repo, 'topic'), tmp_path / 'runs')
    git(repo, 'checkout', '-q', 'main')
    git(repo, 'branch', '-qD', 'topic')
    git(repo, 'reflog', 'expire', '--expire=now', '--all')
    git(repo, 'gc', '-q', '--prune=now')
    assert subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', head]).returncode != 0   # gone there
    ws = Path(out['workspace'])
    git(ws, 'fsck', '--no-dangling')
    assert git(ws, 'rev-parse', 'HEAD') == head and (ws / 'file').read_text() == 'unpushed\n'


def test_d2_home_hooks_and_smudge_filters_do_not_run(setup, tmp_path, monkeypatch):
    repo, _, base, *_ = setup
    sentinel = tmp_path / 'ran'
    hooks = tmp_path / 'home-hooks'
    hooks.mkdir()
    for hook in ('post-checkout', 'reference-transaction', 'post-merge'):
        (hooks / hook).write_text(f'#!/bin/sh\necho {hook} >> {sentinel}\n')
        (hooks / hook).chmod(0o755)
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.gitconfig').write_text(f'[core]\n\thooksPath = {hooks}\n[filter "x"]\n'
                                     f'\tsmudge = sh -c "echo smudge >> {sentinel}; cat"\n\trequired = true\n')
    head = commit(repo, '.gitattributes', '* filter=x\n', 'attributes name a driver')
    real_home = os.environ['HOME']
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    out = mp.materialize(mp.resolve(repo, 'topic'), tmp_path / 'runs')
    assert not sentinel.exists()
    monkeypatch.setenv('HOME', real_home)   # the test's own git calls below must not use that HOME
    assert_materialized(out, tmp_path / 'runs', head, base)


def test_d2_remove_needs_the_marker(setup, tmp_path):
    out = mp.materialize(mp.resolve(setup[0], 'topic'), tmp_path / 'runs')
    ws = Path(out['workspace'])
    plain = tmp_path / 'runs' / 'pr' / ('0' * 32)
    (plain / '.git').mkdir(parents=True)
    link = tmp_path / 'runs' / 'pr' / ('1' * 32)
    link.symlink_to(ws)
    wrong = tmp_path / 'elsewhere' / ws.name
    (wrong / '.git').mkdir(parents=True)
    (wrong / '.git' / mp.MARKER).write_text(ws.name + '\n')
    for path in (plain, link, wrong, tmp_path / 'missing'):
        with pytest.raises(mp.MaterializeError):
            mp.remove(path)
    assert plain.is_dir() and link.is_symlink() and wrong.is_dir() and ws.is_dir()
    (ws / '.git' / mp.MARKER).write_text('f' * 32 + '\n')   # a marker for another run
    with pytest.raises(mp.MaterializeError, match='marker'):
        mp.remove(ws)
    (ws / '.git' / mp.MARKER).write_text(ws.name + '\n')
    assert mp.remove(ws) == str(ws.resolve()) and not ws.exists()


def test_d2_a_clone_killed_before_its_marker_leaves_nothing(setup, tmp_path, monkeypatch):   # R1
    original = mp.command

    def clone_then_time_out(argv, cwd, **kwargs):
        out = original(argv, cwd, **kwargs)
        if 'clone' in argv:   # the clone has written .git and objects, then its timeout kills it
            raise mp.ResolutionError('git unavailable or timed out')
        return out
    monkeypatch.setattr(mp, 'command', clone_then_time_out)
    with pytest.raises(mp.MaterializeError, match='clone failed'):
        mp.materialize(mp.resolve(setup[0], 'topic'), tmp_path / 'runs')
    assert list((tmp_path / 'runs' / 'pr').iterdir()) == []


def test_d2_root_inside_the_operator_repository_is_refused(setup, tmp_path):   # R1
    repo = setup[0]
    resolution = mp.resolve(repo, 'topic')
    linked = tmp_path / 'linked-root'
    linked.mkdir()
    (linked / 'pr').symlink_to(repo / '.git')
    before = operator_state(repo)
    for root in (repo, repo / 'sub', repo / '.git', repo / '.git' / 'x', linked):
        with pytest.raises(mp.MaterializeError, match='inside the operator repository'):
            mp.materialize(resolution, root)
    assert operator_state(repo) == before and not (repo / 'pr').exists() and not (repo / 'sub').exists()


@pytest.fixture
def private_remote(setup, tmp_path, monkeypatch):
    """A private-remote stand-in: `git http-backend` behind Basic auth on 127.0.0.1 (plain http, so the test widens
    ALLOWED_PROTOCOLS; production keeps https), with a fake `gh auth git-credential` as the only credential source."""
    import base64
    import http.server
    import threading
    repo, target, *_ = setup
    git(repo, 'push', '-q', str(target), 'topic:refs/heads/topic')
    token = 'Basic ' + base64.b64encode(b'reviewer:FAKE-PAT').decode()
    project_root = str(target.parent)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.serve()

        def do_POST(self):
            self.serve()

        def serve(self):
            if self.headers.get('Authorization') != token:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="private"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            path, _, query = self.path.partition('?')
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            env = {**os.environ, 'GIT_PROJECT_ROOT': project_root, 'GIT_HTTP_EXPORT_ALL': '1', 'PATH_INFO': path,
                   'QUERY_STRING': query, 'REQUEST_METHOD': self.command, 'REMOTE_USER': 'reviewer',
                   'REMOTE_ADDR': '127.0.0.1', 'CONTENT_TYPE': self.headers.get('Content-Type', ''),
                   'CONTENT_LENGTH': str(len(body))}
            for header, name in (('Git-Protocol', 'GIT_PROTOCOL'), ('Content-Encoding', 'HTTP_CONTENT_ENCODING')):
                if self.headers.get(header):
                    env[name] = self.headers[header]
            out = subprocess.run(['git', 'http-backend'], input=body, env=env, capture_output=True).stdout
            head, _, payload = out.partition(b'\r\n\r\n') if b'\r\n\r\n' in out else out.partition(b'\n\n')
            status, headers = 200, []
            for line in head.decode().splitlines():
                key, _, value = line.partition(':')
                if key.lower() == 'status':
                    status = int(value.split()[0])
                else:
                    headers.append((key, value.strip()))
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_address[1]}/{target.name}'
    bindir = tmp_path / 'cred-bin'
    bindir.mkdir()
    log, creds = tmp_path / 'cred.log', tmp_path / 'cred.txt'
    (bindir / 'gh').write_text('#!' + sys.executable + '\nimport sys\nfrom pathlib import Path\n'
                               f'Path({str(log)!r}).open("a").write(" ".join(sys.argv[1:]) + "\\n")\n'
                               'if sys.argv[1:3] == ["auth", "git-credential"] and sys.argv[3] == "get":\n'
                               '    sys.stdin.read()\n'
                               f'    if Path({str(creds)!r}).exists(): print(Path({str(creds)!r}).read_text())\n')
    (bindir / 'gh').chmod(0o755)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ['PATH'])
    monkeypatch.setattr(mp, 'ALLOWED_PROTOCOLS', (*mp.ALLOWED_PROTOCOLS, 'http'))
    yield url, log, creds
    server.shutdown()


def test_d2_private_remote_clones_and_fetches_through_the_gh_helper(setup, tmp_path, private_remote):
    repo, _, base, head, *_ = setup
    url, log, creds = private_remote
    resolution = {'kind': 'ref', 'operator_repo': str(repo), 'target_url': url,
                  'head': {'oid': head, 'source': url, 'fetch_ref': 'refs/heads/topic'},
                  'base': {'oid': base, 'source': url, 'fetch_ref': 'refs/heads/main'}}
    creds.write_text('username=reviewer\npassword=FAKE-PAT\n')
    out = mp.materialize(resolution, tmp_path / 'runs')
    ws = assert_materialized(out, tmp_path / 'runs', head, base)
    assert 'auth git-credential get' in log.read_text()
    assert 'FAKE-PAT' not in (ws / '.git' / 'config').read_text()   # nothing stored
    creds.unlink()   # the helper has no credential: fail closed, no prompt (stdin is closed; prompts are off)
    with pytest.raises(mp.MaterializeError, match='clone failed'):
        mp.materialize(resolution, tmp_path / 'runs')
    assert [p for p in (tmp_path / 'runs' / 'pr').iterdir()] == [ws]


def test_d2_cli_materializes_and_removes(setup, tmp_path):
    script = Path(mp.__file__).resolve()

    def run(*args):
        return subprocess.run([sys.executable, str(script), *args], text=True, capture_output=True)
    made = run('topic', '--repo', str(setup[0]), '--root', str(tmp_path / 'runs'))
    assert made.returncode == 0, made.stderr
    ws = json.loads(made.stdout)['workspace']
    assert git(Path(ws), 'rev-parse', 'HEAD') == setup[3]
    gone = run('--remove', ws)
    assert gone.returncode == 0 and not Path(ws).exists()
    refused = run('--remove', str(tmp_path))
    assert refused.returncode == 2 and 'REFUSED:' in refused.stderr and tmp_path.exists()
    assert run('topic', '--remove', ws).returncode == 2   # an input and --remove together
