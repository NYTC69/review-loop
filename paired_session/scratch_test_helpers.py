"""WI-HYG: keep the per-run scratch roots that tests create out of the real <temp>/review-loop-<user>/."""
import os
import shutil
import stat
import tempfile
import warnings
from pathlib import Path
from unittest.mock import patch

from paired_session import winplat

ENV = 'PAIRED_SESSION_INTERNAL_TEST_SCRATCH_PARENT'


def _remove(path):
    if not os.path.isdir(path):
        return
    os.chmod(path, stat.S_IRWXU)
    for folder, dirs, files in os.walk(path):   # a test may leave a scratch directory or file read-only (Windows: undeletable)
        for name in dirs + files:
            child = os.path.join(folder, name)
            if not os.path.islink(child):
                try:
                    os.chmod(child, stat.S_IRWXU)
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        warnings.warn(f'WI-HYG: could not remove the test scratch parent {path}', RuntimeWarning)


def isolate_scratch(test):
    """Point new_scratch_root (in process and in the coordinators a test starts) at a private parent of this test,
    outside every git work tree, and remove it with the test."""
    parent = Path(tempfile.mkdtemp(prefix='review-loop-test-scratch-', dir=winplat.temp_root())).resolve()
    test.addCleanup(_remove, parent)
    env = patch.dict(os.environ, {ENV: str(parent)})
    env.start()
    test.addCleanup(env.stop)
    return parent
