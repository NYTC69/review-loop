"""Coordinator-owned temporary directories with no-follow, descriptor-based cleanup."""
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


def _identity(info):
    return info.st_dev, info.st_ino


def _empty(fd):
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if _identity(os.fstat(child)) != _identity(info):
                    raise RuntimeError('temporary directory changed during cleanup')
                _empty(child)
            finally:
                os.close(child)
            if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(info):
                raise RuntimeError('temporary child replaced during cleanup')
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


@contextmanager
def directory(*, dir=None, prefix='paired-session-tmp-'):
    path = Path(tempfile.mkdtemp(dir=dir, prefix=prefix)).absolute()
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root = None
    try:
        root = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        identity = _identity(os.fstat(root))
        yield str(path)
    finally:
        try:
            if root is not None:
                if _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != identity:
                    raise RuntimeError('temporary root replaced; inspect before cleanup')
                _empty(root)
                if _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != identity:
                    raise RuntimeError('temporary root changed during cleanup')
                os.rmdir(path.name, dir_fd=parent)
        finally:
            if root is not None:
                os.close(root)
            os.close(parent)
