"""Handle-bound, same-volume, no-replace file moves for the Windows preview."""
import errno
import os
import shutil
import stat
import tempfile

from . import native
from .io import os as fs
from .paths import absolute, component
from ..atomic_move import (AtomicMoveUnavailable, DestinationExists, SourceMissing,
    SourceChangedError, MoveFailed, FileIdentity)


def identity(st):
    return FileIdentity(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                        'dir' if stat.S_ISDIR(st.st_mode) else 'regular')


def translate(exc):
    code = getattr(exc, 'winerror', None)
    if code in (80, 183):
        return DestinationExists('目标已存在，跳过且不覆盖。')
    if code in (2, 3):
        return SourceMissing('源或父目录已不存在。')
    if code == 17:
        return MoveFailed(errno.EXDEV, '跨卷移动已拒绝。')
    if code in (32, 33):
        return MoveFailed(errno.EBUSY, '文件被占用，本项未移动。')
    if code == 5:
        return MoveFailed(errno.EACCES, '权限不足，未移动。')
    return MoveFailed(errno.EIO, 'Windows 移动失败，请检查记录。')


class WindowsAtomicMover:
    def __init__(self, *args, **kwargs):
        try:
            native.api()
            self.available, self.unavailable_reason = True, ''
        except OSError as exc:
            self.available, self.unavailable_reason = False, str(exc)

    def _require(self):
        if not self.available:
            raise AtomicMoveUnavailable(self.unavailable_reason)

    def rename_excl_at(self, source_fd, source_name, dest_fd, dest_name, *, expected=None):
        self._require()
        if expected is None:
            raise AtomicMoveUnavailable('Windows 移动必须绑定已确认的源身份。')
        component(source_name); component(dest_name)
        handle = None
        try:
            if native.info(source_fd.handle).st_dev != native.info(dest_fd.handle).st_dev:
                raise MoveFailed(errno.EXDEV, '跨卷移动已拒绝。')
            handle = native.nt_open(source_fd.handle, source_name,
                access=native.READ_ATTR | native.DELETE, share=1)
            before = identity(native.info(handle))
            if before.mode_type != 'regular' or not before.matches(expected):
                raise SourceChangedError('源文件身份已变化，未移动。')
            native.rename(handle, dest_fd, dest_name)
            # A failure after rename is uncertain, never classified as a safely
            # skipped item. The journal's pending intent supports recovery.
            try:
                if not identity(native.info(handle)).matches(expected):
                    raise RuntimeError('移动后句柄身份不符，需恢复检查。')
            except OSError as exc:
                raise RuntimeError('移动后身份读取失败，需恢复检查。') from exc
        except OSError as exc:
            raise translate(exc) from exc
        finally:
            native.close(handle)

    def rename_excl(self, source, dest):
        # Only artificial capability probes use the unbound path convenience API.
        raise AtomicMoveUnavailable('Windows 禁止未绑定身份的路径移动。')

    def probe_capability(self, work_dir, tag='probe'):
        self._require()
        absolute(work_dir)
        with native.open_directory(work_dir):
            pass
        root = tempfile.mkdtemp(prefix='.windows-probe-', dir=work_dir)
        try:
            a, b = root + '/a', root + '/b'
            for p, data in ((a, b'probe-a'), (b, b'probe-b')):
                with open(p, 'xb') as out:
                    out.write(data); out.flush(); os.fsync(out.fileno())
            with native.open_directory(root) as directory:
                before = identity(fs.lstat(a)).as_dict()
                try:
                    self.rename_excl_at(directory, 'a', directory, 'b', expected=before)
                except DestinationExists:
                    pass
                else:
                    raise AtomicMoveUnavailable('Windows 覆盖拒绝探测失败。')
                with open(a, 'rb') as check_a, open(b, 'rb') as check_b:
                    if check_a.read() != b'probe-a' or check_b.read() != b'probe-b':
                        raise AtomicMoveUnavailable('探测文件被改变。')
                self.rename_excl_at(directory, 'a', directory, 'c', expected=before)
                if fs.path.exists(a) or not identity(fs.lstat(root + '/c')).matches(before):
                    raise AtomicMoveUnavailable('Windows 移动身份探测失败。')
                self.rename_excl_at(directory, 'c', directory, 'a', expected=before)
        except Exception as exc:
            raise AtomicMoveUnavailable('Windows 安全移动能力探测失败。') from exc
        finally:
            # Only this invocation's fresh artificial files; never user files.
            shutil.rmtree(root)

    def probe_directory_capability(self, work_dir):
        raise AtomicMoveUnavailable('Windows 内测版不支持目录归档。')
