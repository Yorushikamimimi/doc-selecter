"""Darwin 原子不覆盖移动原语封装。

唯一允许的移动机制是 ``renamex_np`` / ``renameatx_np`` + ``RENAME_EXCL``
（docs/MVP方案.md §2.4）：

- 目标不存在 → 单次原子移动成功，源消失、目标是同一 inode；
- 目标已存在（文件或目录）→ 失败 ``EEXIST``，**双方都不被改动**。

**禁止降级**：不退回 ``os.rename``（会静默覆盖）、不退回「检查 + rename」、
不使用 ``link`` + ``unlink``。原语不可用时调用方必须**拒绝移动**。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import tempfile
from dataclasses import dataclass


class AtomicMoveError(RuntimeError):
    """原子移动相关错误基类。"""


class AtomicMoveUnavailable(AtomicMoveError):
    """当前环境不提供 RENAME_EXCL 原子不覆盖语义 → 必须拒绝移动。"""


class OutOfScopeError(AtomicMoveError):
    """路径越界或路径段是符号链接。"""


class DirectoryIdentityChanged(AtomicMoveError):
    """目录身份（st_dev/st_ino）与预期不符。"""


class SourceChangedError(AtomicMoveError):
    """源文件身份与预期不符。"""


class MoveFailed(AtomicMoveError):
    """原语返回了未归类错误（已确认未发生移动）。"""

    def __init__(self, errno_value: int, message: str) -> None:
        super().__init__(message)
        self.errno = errno_value


class DestinationExists(AtomicMoveError):
    """目标已存在（含竞态中被外部创建）→ 跳过，绝不覆盖。"""


class SourceMissing(AtomicMoveError):
    """源不存在。"""


RENAME_EXCL = 0x00000004  # Darwin <stdio.h>；RENAME_SWAP = 0x2, RENAME_EXCL = 0x4

#: 目录 fd 打开标志
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)


def _load_libc() -> ctypes.CDLL:
    for name in (ctypes.util.find_library("c"), "libc.dylib", "/usr/lib/libc.dylib"):
        if not name:
            continue
        try:
            return ctypes.CDLL(name, use_errno=True)
        except OSError:
            continue
    raise AtomicMoveUnavailable("无法加载 libc")


class AtomicMover:
    """``renamex_np`` / ``renameatx_np`` 的薄封装。"""

    def __init__(self, libc: ctypes.CDLL | None = None) -> None:
        self._path_fn = None
        self._at_fn = None
        self._available = False
        self._unavailable_reason = ""
        try:
            libc = libc or _load_libc()
        except AtomicMoveUnavailable as exc:
            self._unavailable_reason = str(exc)
            return
        path_fn = getattr(libc, "renamex_np", None)
        at_fn = getattr(libc, "renameatx_np", None)
        if path_fn is None or at_fn is None:
            self._unavailable_reason = (
                "libc 中缺少 renamex_np/renameatx_np（RENAME_EXCL 不可用）"
            )
            return
        path_fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        path_fn.restype = ctypes.c_int
        at_fn.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint
        ]
        at_fn.restype = ctypes.c_int
        self._path_fn = path_fn
        self._at_fn = at_fn
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    def _require(self) -> None:
        if not self._available:
            raise AtomicMoveUnavailable(
                self._unavailable_reason or "RENAME_EXCL 不可用"
            )

    # --- 原语 ---------------------------------------------------------------
    def rename_excl(self, source: str, dest: str) -> None:
        """路径版原子不覆盖移动。"""
        self._require()
        ctypes.set_errno(0)  # type: ignore[attr-defined]
        result = self._path_fn(  # type: ignore[misc]
            os.fsencode(source), os.fsencode(dest), RENAME_EXCL
        )
        if result != 0:
            err = ctypes.get_errno()
            raise _translate(err, source, dest)

    def rename_excl_at(self, source_fd: int, source_name: str, dest_fd: int, dest_name: str) -> None:
        """fd 版原子不覆盖移动（消除路径解析竞态）。"""
        self._require()
        ctypes.set_errno(0)  # type: ignore[attr-defined]
        result = self._at_fn(  # type: ignore[misc]
            source_fd, os.fsencode(source_name), dest_fd, os.fsencode(dest_name), RENAME_EXCL
        )
        if result != 0:
            err = ctypes.get_errno()
            raise _translate(err, source_name, dest_name)

    # --- 能力探测 -----------------------------------------------------------
    def probe_capability(self, work_dir: str, tag: str = "probe") -> None:
        """按 §2.4.4 在 ``work_dir``（必须是状态目录）内做两步能力探测。

        1. 目标不存在 → 必须成功；
        2. 先排他新建一个新的人工探测源（因为第 1 步后源已消失），确认源与目标
           同时存在，再次调用必须失败且 ``errno == EEXIST``，且探测文件的内容与
           元数据均未改变。

        **隔离与清理（P1 修复）**：每次调用都用 ``mkdtemp`` 新建一个**唯一**的
        私有探测目录 ``.probe-<tag>-<随机>/``，所有探测文件都建在其中：

        - **绝不按名字删除**任何历史或未知的 ``.probe-*`` 文件；
        - 只清理**本次创建**的目录（先删除目录内文件再 ``rmdir``）；
        - 唯一目录同时消除了并发探测互相干扰（同名文件冲突）的风险；
        - 清理失败只记录，不掩盖真正的探测结论。

        任一步不符 → :class:`AtomicMoveUnavailable`。
        """
        self._require()
        if not os.path.isdir(work_dir):
            raise AtomicMoveUnavailable(f"能力探测目录不存在：{work_dir}")

        probe_dir = tempfile.mkdtemp(prefix=f".{tag}-", dir=work_dir)
        src_a = os.path.join(probe_dir, "src-a")
        dst_a = os.path.join(probe_dir, "dst-a")
        src_b = os.path.join(probe_dir, "src-b")
        dst_b = os.path.join(probe_dir, "dst-b")
        payload = b"doc-selecter capability probe"
        created: list[str] = []

        def make_exclusive(path: str) -> None:
            fd = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            created.append(path)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)

        try:
            # 步骤 1：目标不存在必须成功
            make_exclusive(src_a)
            try:
                self.rename_excl(src_a, dst_a)
            except AtomicMoveUnavailable:
                raise
            except AtomicMoveError as exc:
                raise AtomicMoveUnavailable(
                    f"能力探测第 1 步失败（目标不存在时应成功）：{exc}"
                ) from exc
            if os.path.lexists(src_a) or not os.path.isfile(dst_a):
                raise AtomicMoveUnavailable("能力探测第 1 步结果异常：源未消失或目标未出现")
            if _read_all(dst_a) != payload:
                raise AtomicMoveUnavailable("能力探测第 1 步结果异常：目标内容与预期不符")

            # 步骤 2：源与目标同时存在，必须失败且 EEXIST，且双方不变
            make_exclusive(src_b)
            make_exclusive(dst_b)
            before = (
                _stat_signature(src_b), _stat_signature(dst_b),
                _read_all(src_b), _read_all(dst_b),
            )
            try:
                self.rename_excl(src_b, dst_b)
            except DestinationExists:
                pass  # 期望结果
            except AtomicMoveUnavailable:
                raise
            except AtomicMoveError as exc:
                raise AtomicMoveUnavailable(
                    f"能力探测第 2 步失败（目标存在时应 EEXIST）：{exc}"
                ) from exc
            else:
                raise AtomicMoveUnavailable(
                    "能力探测第 2 步失败：目标已存在却仍然移动成功（会覆盖，拒绝使用）"
                )
            after = (
                _stat_signature(src_b), _stat_signature(dst_b),
                _read_all(src_b), _read_all(dst_b),
            )
            if before != after:
                raise AtomicMoveUnavailable("能力探测第 2 步失败：失败后探测文件发生了改变")
        finally:
            # 只清理**本次**创建的目录；绝不触碰其他 .probe-* 文件
            for path in [src_a, dst_a, src_b, dst_b] + created:
                try:
                    if os.path.lexists(path):
                        os.unlink(path)
                except OSError:
                    pass
            try:
                os.rmdir(probe_dir)
            except OSError:
                pass


def _read_all(path: str, limit: int = 4096) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(limit)


def _stat_signature(path: str) -> tuple[int, int, int, int]:
    st = os.lstat(path)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _translate(err: int, source: str, dest: str) -> AtomicMoveError:
    if err == errno.EEXIST:
        return DestinationExists(
            f"目标已存在，原子不覆盖移动被拒绝：{os.fsdecode(dest)}"
        )
    if err == errno.ENOENT:
        return SourceMissing(f"源或路径段不存在：{os.fsdecode(source)}")
    if err == errno.ENOSYS or err == errno.EINVAL:
        return AtomicMoveUnavailable(
            f"原语不支持 RENAME_EXCL（errno={errno.errorcode.get(err, err)}）"
        )
    if err == errno.EXDEV:
        return MoveFailed(err, f"跨文件系统（EXDEV），拒绝移动：{os.fsdecode(source)}")
    if err in (errno.EISDIR, errno.ENOTDIR):
        return MoveFailed(err, f"路径类型冲突（errno={errno.errorcode.get(err, err)}）")
    if err in (errno.EACCES, errno.EPERM):
        return MoveFailed(err, f"权限不足（errno={errno.errorcode.get(err, err)}）")
    return MoveFailed(err, f"原子移动失败（errno={errno.errorcode.get(err, err)}）")


# --- 目录 fd 助手 --------------------------------------------------------------
def open_dir_fd(path: str) -> int:
    """以 ``O_DIRECTORY`` 打开目录；若任一路径段是符号链接则拒绝。"""
    if os.path.islink(path):
        raise OutOfScopeError(f"拒绝：路径是符号链接（{path}）")
    if not os.path.isdir(path):
        raise DirectoryIdentityChanged(f"目录不存在：{path}")
    return os.open(path, _DIR_FLAGS)


def dir_identity(path: str) -> tuple[int, int]:
    st = os.lstat(path)
    return (st.st_dev, st.st_ino)


def fd_identity(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def assert_no_symlink_segments(base: str, target: str) -> None:
    """确保 ``target`` 相对 ``base`` 的每一段都不是符号链接（含最后一段）。"""
    base = os.path.normpath(base)
    target = os.path.normpath(target)
    if not (target == base or target.startswith(base + os.sep)):
        raise OutOfScopeError(f"路径越界：{target} 不在 {base} 之下")
    rel = os.path.relpath(target, base)
    if rel in (".", ""):
        return
    current = base
    for part in rel.split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise OutOfScopeError(f"拒绝：路径段是符号链接（{current}）")


def assert_no_symlink_segment_for_file(target: str, base: str) -> None:
    """确保文件 ``target`` 的**父目录链**（相对 ``base``）都不是符号链接。

    文件自身是否存在、是否为链接由调用方用 ``lstat`` 判定（避免 TOCTOU 误判）。
    """
    assert_no_symlink_segments(base, os.path.dirname(os.path.normpath(target)))


@dataclass(frozen=True)
class FileIdentity:
    st_dev: int
    st_ino: int
    size_bytes: int
    mtime_ns: int
    mode_type: str

    def as_dict(self) -> dict[str, object]:
        return {
            "st_dev": self.st_dev,
            "st_ino": self.st_ino,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "mode_type": self.mode_type,
        }

    def matches(self, other: object) -> bool:
        if not isinstance(other, dict):
            return False
        try:
            return (
                int(other["st_dev"]) == self.st_dev
                and int(other["st_ino"]) == self.st_ino
                and int(other["size_bytes"]) == self.size_bytes
                and int(other["mtime_ns"]) == self.mtime_ns
                and str(other["mode_type"]) == self.mode_type
            )
        except (KeyError, TypeError, ValueError):
            return False


def mode_type_of(st_mode: int) -> str:
    import stat as statmod

    if statmod.S_ISREG(st_mode):
        return "regular"
    if statmod.S_ISDIR(st_mode):
        return "dir"
    if statmod.S_ISLNK(st_mode):
        return "symlink"
    return "other"


def lstat_identity(path: str) -> FileIdentity | None:
    """``lstat`` 出五项身份；路径不存在返回 ``None``；其他错误必须抛出。

    只有 ``FileNotFoundError``/``ENOENT`` 表示「不存在」——权限与 I/O 错误
    一律抛出，绝不能被当作「不存在」（§6.2 第 5 条）。
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return FileIdentity(
        st_dev=st.st_dev,
        st_ino=st.st_ino,
        size_bytes=st.st_size,
        mtime_ns=st.st_mtime_ns,
        mode_type=mode_type_of(st.st_mode),
    )


def matches_identity(path: str, expected: dict[str, object]) -> bool:
    """路径存在、是普通文件，且五项身份与 ``expected`` 完全一致。"""
    actual = lstat_identity(path)
    if actual is None:
        return False
    if actual.mode_type != "regular":
        return False
    return actual.matches(expected)
