import hashlib, json, os, signal, subprocess, sys, tempfile
from pathlib import Path
from importlib import import_module
spine = import_module(('paired_session.' if __package__ else '') + 'lifecycle_spine')
def run(co, command, *, cwd, env, timeout, capture_output=True):
    root = Path(cwd).resolve(strict=True)
    fake_root = Path(os.environ.get('FAKE_CODEX_TEST_ROOT', '/')).resolve()
    engine = Path('/usr/bin/sandbox-exec')
    if (not co._fake_lifecycle or not spine.fake_dispatch_guard(co.args) or not capture_output or
            fake_root not in root.parents or any(p == root or p in root.parents
            for p in (co.workspace, co.run_dir)) or sys.platform != 'darwin' or not engine.is_file()):
        raise RuntimeError('candidate test write sandbox unavailable or invalid; refuse dispatch')
    with tempfile.TemporaryDirectory(dir=co.evidence, prefix='test-tmp-') as scratch:
        tmp = Path(scratch).resolve()
        paths = ' '.join('(subpath ' + json.dumps(str(p), ensure_ascii=False) + ')' for p in (root, tmp))
        protected = ' '.join('(subpath ' + json.dumps(str(root / p), ensure_ascii=False) + ')'
                             for p in ('BACKLOG.md', '.compass', '.git'))
        profile = ('(version 1)(allow default)(deny file-write*)(allow file-write* ' + paths +
                   ')(deny file-write* ' + protected + ')(allow file-write-data (literal "/dev/null"))'
                   '(deny file-link)(deny network*)(deny mach-lookup)')
        argv = [str(engine), '-p', profile, *(command or ['/usr/bin/true'])]
        process = subprocess.Popen(argv, cwd=root, env={**env, 'TMPDIR': str(tmp),
                    'GIT_CEILING_DIRECTORIES': str(root.parent)}, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
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
