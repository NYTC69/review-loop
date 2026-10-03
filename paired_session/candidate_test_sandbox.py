import hashlib, json, os, pwd, signal, subprocess, sys, tempfile
from pathlib import Path
from importlib import import_module
safe_temp = import_module(('paired_session.' if __package__ else '') + 'safe_temp')
spine = import_module(('paired_session.' if __package__ else '') + 'lifecycle_spine')
def run(co, command, *, cwd, env, timeout, capture_output=True):
    root = Path(cwd).resolve(strict=True)
    root_text = os.environ.get('FAKE_CODEX_TEST_ROOT')
    if not root_text:
        raise RuntimeError('candidate test root missing; refuse dispatch')
    if not Path(root_text).is_absolute():
        raise RuntimeError('candidate test root must be absolute; refuse dispatch')
    fake_root = Path(root_text).resolve(strict=True)
    broad_roots = (Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(),
                   Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve(), Path('/var/tmp').resolve())
    broad_scope = any(fake_root == p or fake_root in p.parents for p in broad_roots)
    engine = Path('/usr/bin/sandbox-exec')
    protected_roots = (co.workspace.resolve(), co.run_dir.resolve())
    overlaps = any(p == root or p in root.parents or root in p.parents for p in protected_roots)
    if (broad_scope or not co._fake_lifecycle or
            not spine.fake_dispatch_guard(co.args) or not capture_output or fake_root not in root.parents or
            overlaps or sys.platform != 'darwin' or not engine.is_file()):
        raise RuntimeError('candidate test write sandbox unavailable or invalid; refuse dispatch')
    with safe_temp.directory(dir=co.evidence, prefix='test-tmp-') as scratch:
        tmp = Path(scratch).resolve()
        paths = ' '.join('(subpath ' + json.dumps(str(p), ensure_ascii=False) + ')' for p in (root, tmp))
        protected = ' '.join('(subpath ' + json.dumps(str(root / p), ensure_ascii=False) + ')'
                             for p in ('BACKLOG.md', '.compass', '.git'))
        profile = ('(version 1)(allow default)(deny file-write*)(allow file-write* ' + paths +
                   ')(deny file-write* ' + protected + ')(allow file-write-data (literal "/dev/null"))'
                   '(deny file-link)(deny network*)(deny mach-lookup)')
        argv = [str(engine), '-p', profile, *(command or ['/usr/bin/true'])]
        process = subprocess.Popen(argv, cwd=root, env={**env, 'TMPDIR': str(tmp),
                    'GIT_CEILING_DIRECTORIES': str(root.parent)}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # macOS refuses signals to a zombie-only group: reap our child, then use the
                # coordinator's bounded EPERM retry until the group is gone (fail closed otherwise).
                process.poll()
                try:
                    import_module(('paired_session.' if __package__ else '') + 'coordinator').retry_killpg_eperm(process.pid)
                except ProcessLookupError:
                    pass
                else:
                    raise RuntimeError('candidate test process group is still alive; refuse dispatch')
            if process.poll() is None:
                process.wait()
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        if command is None and result.returncode:
            raise RuntimeError('candidate test sandbox unavailable; refuse dispatch')
        result.write_boundary = {'engine': str(engine),
                                 'engine_sha256': hashlib.sha256(engine.read_bytes()).hexdigest(),
                                 'profile_sha256': hashlib.sha256(profile.encode()).hexdigest(),
                                 'root': str(root), 'tmpdir': str(tmp)}
        return result
