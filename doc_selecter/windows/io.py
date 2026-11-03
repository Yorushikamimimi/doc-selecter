"""Narrow adapter for the engine's filesystem calls; not a global monkeypatch.

Directory descriptors are owned Directory objects. File descriptors are real CRT
fds with pinned parent chains. Payload opens are used only by existing state/log
code and synthetic probes; scanning queries metadata through native handles.
"""
import builtins
import errno
import ntpath
import os as _os
import stat as _stat
import threading
from contextlib import contextmanager

from . import native
from .paths import absolute, component

_parents = {}
_mutex = threading.RLock()
O_DIRECTORY = 0x10000000
O_NOFOLLOW = 0x20000000


def _release(fd):
    with _mutex:
        parent = _parents.pop(fd, None)
    if parent:
        parent.close()


class File:
    def __init__(self, file, fd):
        self._file, self._fd = file, fd

    def __getattr__(self, name):
        return getattr(self._file, name)

    def __iter__(self):
        return iter(self._file)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            self._file.close()
        finally:
            _release(fd)


class Path:
    def __getattr__(self, name):
        fn = getattr(ntpath, name)
        if name in {'join', 'normpath', 'abspath', 'dirname', 'relpath', 'expanduser', 'commonpath'}:
            return lambda *a, **kw: fn(*a, **kw).replace('\\', '/')
        return fn

    def realpath(self, path):
        # Never resolve reparse points to construct a trust anchor.
        return absolute(_os.fspath(path))

    def _is(self, path, kind):
        try:
            st = os.lstat(path)
            return kind(st.st_mode)
        except FileNotFoundError:
            return False

    def isfile(self, path):
        return self._is(path, _stat.S_ISREG)

    def isdir(self, path):
        return self._is(path, _stat.S_ISDIR)

    def exists(self, path):
        return self._is(path, lambda mode: True)

    lexists = exists

    def islink(self, path):
        # Native opens reject *all* reparse points, including junctions, instead
        # of following them or reporting an inaccessible path as nonexistent.
        try:
            os.lstat(path)
        except FileNotFoundError:
            pass
        return False


class Entry:
    def __init__(self, parent, name):
        self.parent = parent
        self.name, self.path = name, parent.path.rstrip('/') + '/' + name

    def stat(self, *, follow_symlinks=True):
        return os.stat(self.name, dir_fd=self.parent, follow_symlinks=False)

    def is_dir(self, *, follow_symlinks=True):
        return _stat.S_ISDIR(self.stat().st_mode)

    def is_file(self, *, follow_symlinks=True):
        return _stat.S_ISREG(self.stat().st_mode)

    def is_symlink(self):
        return bool(self.stat().st_file_attributes & native.REPARSE)


class Entries:
    def __init__(self, values):
        self.values = iter(values)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.values)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        pass


class OS:
    name, sep, altsep = 'nt', '/', '\\'
    path = Path()
    O_DIRECTORY, O_NOFOLLOW, O_CLOEXEC = O_DIRECTORY, O_NOFOLLOW, 0

    def __getattr__(self, name):
        return getattr(_os, name)

    def lstat(self, path):
        path = absolute(_os.fspath(path))
        if len(path) == 3:
            with native.open_directory(path) as directory:
                return native.info(directory.handle)
        parent, name = path.rsplit('/', 1)
        with native.open_directory(parent + ('/' if parent.endswith(':') else '')) as directory:
            return self.stat(name, dir_fd=directory, follow_symlinks=False)

    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is None:
            return self.lstat(path)
        handle = native.nt_open(dir_fd.handle, path, directory=None)
        try:
            return native.info(handle)
        finally:
            native.close(handle)

    def fstat(self, fd):
        if isinstance(fd, native.Directory):
            return native.info(fd.handle)
        import msvcrt
        return native.info(msvcrt.get_osfhandle(fd))

    def open(self, path, flags, mode=0o600, *, dir_fd=None):
        import msvcrt
        if flags & O_DIRECTORY:
            if dir_fd is not None:
                component(path)
                path = dir_fd.path.rstrip('/') + '/' + path
            return native.open_directory(_os.fspath(path))
        owned = None
        if dir_fd is None:
            path = absolute(_os.fspath(path))
            parent, name = path.rsplit('/', 1)
            owned = native.open_directory(parent + ('/' if parent.endswith(':') else ''))
            dir_fd = owned
        else:
            name = component(path)
        writable = bool(flags & (_os.O_WRONLY | _os.O_RDWR))
        access = native.READ_ATTR | (0x40000000 if writable else 0x80000000)
        if flags & _os.O_RDWR:
            access |= 0x80000000
        disposition = 2 if flags & _os.O_EXCL else (3 if flags & _os.O_CREAT else 1)
        handle = None
        fd = None
        try:
            # No truncate through NtCreateFile: exclusive/type/reparse validation
            # happens before any intentional state-file truncation.
            handle = native.nt_open(dir_fd.handle, name, access=access, share=3, disposition=disposition)
            crt_flags = (flags & ~ (O_DIRECTORY | O_NOFOLLOW | _os.O_CREAT | _os.O_EXCL | _os.O_TRUNC)) | _os.O_BINARY
            fd = msvcrt.open_osfhandle(handle, crt_flags)
            handle = None
            with _mutex:
                _parents[fd] = owned
            owned = None
            if flags & _os.O_TRUNC:
                _os.ftruncate(fd, 0)
            return fd
        except BaseException:
            if fd is not None:
                self.close(fd)
            if handle:
                native.close(handle)
            if owned:
                owned.close()
            raise

    def close(self, fd):
        if isinstance(fd, native.Directory):
            return fd.close()
        try:
            _os.close(fd)
        finally:
            _release(fd)

    def fdopen(self, fd, *args, **kwargs):
        try:
            return File(_os.fdopen(fd, *args, **kwargs), fd)
        except BaseException:
            self.close(fd)
            raise

    def fsync(self, fd):
        if isinstance(fd, native.Directory):
            raise OSError('Windows 未证明目录刷盘能力，不能启用目录归档。')
        return _os.fsync(fd)

    def mkdir(self, path, mode=0o700, *, dir_fd=None):
        if dir_fd is None:
            path = absolute(_os.fspath(path))
            parent, name = path.rsplit('/', 1)
            with native.open_directory(parent + ('/' if parent.endswith(':') else '')) as directory:
                return self.mkdir(name, mode, dir_fd=directory)
        handle = native.nt_open(dir_fd.handle, component(path), access=native.READ_ATTR | 1,
                                share=3, disposition=2, directory=True)
        native.close(handle)

    def makedirs(self, path, mode=0o700, exist_ok=False):
        path = absolute(_os.fspath(path))
        if not exist_ok and self.path.exists(path):
            raise FileExistsError(errno.EEXIST, '目录已存在')
        with native.open_directory(path, create=True):
            pass

    def scandir(self, path):
        if isinstance(path, native.Directory):
            directory = native.open_directory(path.path)
        else:
            directory = native.open_directory(_os.fspath(path))
        # Entries retain the parent after scandir's context exits: the scanner
        # deliberately filters sensitive names before asking for metadata.
        return Entries([Entry(directory, name) for name in native.names(directory)])

    def listdir(self, path):
        return [entry.name for entry in self.scandir(path)]

    def replace(self, source, dest):
        """Only existing state writers call this; user moves never replace."""
        source, dest = absolute(_os.fspath(source)), absolute(_os.fspath(dest))
        sp, sn = source.rsplit('/', 1); dp, dn = dest.rsplit('/', 1)
        if sp != dp:
            raise OSError('状态原子替换必须在同一目录内。')
        with native.open_directory(sp) as parent:
            handle = native.nt_open(parent.handle, sn, access=native.READ_ATTR | native.DELETE, share=1)
            try:
                # A reparse-point target is never an allowed replacement.
                try:
                    self.stat(dn, dir_fd=parent)
                except FileNotFoundError:
                    pass
                native.rename(handle, parent, dn, replace=True)
            finally:
                native.close(handle)

    def unlink(self, *args, **kwargs):
        raise OSError('Windows 文件操作适配层不提供删除。')

    remove = unlink


os = OS()


def open(file, mode='r', buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
    if isinstance(file, int) or opener is not None or not closefd:
        raise ValueError('状态读取必须使用受控路径。')
    captured = []
    def create(path, flags):
        fd = os.open(path, flags)
        captured.append(fd)
        return fd
    try:
        stream = builtins.open(file, mode, buffering=buffering, encoding=encoding, errors=errors,
                               newline=newline, opener=create)
        return File(stream, captured[0])
    except BaseException:
        for fd in captured:
            _release(fd)
        raise


def guard(path, *, allow_missing=False):
    path = absolute(path)
    parts = path[3:].split('/') if path[3:] else []
    current = path[:3]
    for index, part in enumerate(parts):
        current = current.rstrip('/') + '/' + part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                return
            raise
        if index < len(parts) - 1 and not _stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError('父路径不是目录。')


@contextmanager
def pinned_directory(path):
    with native.open_directory(path) as directory:
        yield directory
