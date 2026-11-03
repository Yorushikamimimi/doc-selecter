"""Windows 11 x64 / NTFS handle operations. No file payload reads here.

All descendant opens are single-component NtCreateFile calls relative to an
already checked directory. Every ancestor stays open without FILE_SHARE_DELETE.
No MoveFileEx, copy/unlink, delete-on-close or overwrite fallback is provided.
"""
import ctypes as C
import os
import platform
import stat
import struct
import sys
from types import SimpleNamespace

from .paths import absolute, component

U32, U16, U64, I64 = C.c_uint32, C.c_uint16, C.c_uint64, C.c_int64
HANDLE = C.c_void_p
READ_ATTR, DELETE, SYNC = 0x80, 0x10000, 0x100000
SHARE_ALL = 7
REPARSE, DIRECTORY = 0x400, 0x10
UNSAFE_ATTRIBUTES = 0x2 | 0x4 | 0x1000 | 0x40000 | 0x400000


class UNICODE_STRING(C.Structure):
    _fields_ = [('Length', U16), ('MaximumLength', U16), ('Buffer', HANDLE)]


class OBJECT_ATTRIBUTES(C.Structure):
    _fields_ = [('Length', U32), ('RootDirectory', HANDLE), ('ObjectName', C.POINTER(UNICODE_STRING)),
                ('Attributes', U32), ('SecurityDescriptor', HANDLE), ('SecurityQualityOfService', HANDLE)]


class IO_STATUS_BLOCK(C.Structure):
    _fields_ = [('Status', HANDLE), ('Information', C.c_size_t)]


class BASIC(C.Structure):
    _fields_ = [('CreationTime', I64), ('LastAccessTime', I64), ('LastWriteTime', I64),
                ('ChangeTime', I64), ('FileAttributes', U32)]


class STANDARD(C.Structure):
    _fields_ = [('AllocationSize', I64), ('EndOfFile', I64), ('NumberOfLinks', U32),
                ('DeletePending', C.c_ubyte), ('Directory', C.c_ubyte)]


class ID_INFO(C.Structure):
    _fields_ = [('VolumeSerialNumber', U64), ('FileId', C.c_ubyte * 16)]


class RENAME(C.Structure):
    _fields_ = [('Flags', U32), ('RootDirectory', HANDLE), ('FileNameLength', U32), ('FileName', U16 * 1)]


class OVERLAPPED(C.Structure):
    _fields_ = [('Internal', C.c_size_t), ('InternalHigh', C.c_size_t),
                ('Offset', U32), ('OffsetHigh', U32), ('hEvent', HANDLE)]


_api = None


def api():
    global _api
    if _api is not None:
        return _api
    if (os.name != 'nt' or C.sizeof(HANDLE) != 8 or
            platform.machine().lower() not in ('amd64', 'x86_64') or sys.getwindowsversion().build < 22000):
        raise OSError('仅支持 Windows 11 x64。')
    if sys.getwindowsversion().product_type != 1 and not os.environ.get('DOCSELECTER_TEST_ROOT'):
        raise OSError('Windows Server 仅允许人工沙盒中的接口回归，不属于支持系统。')
    k = C.WinDLL('kernel32', use_last_error=True)
    n = C.WinDLL('ntdll')
    machine_check = k.IsWow64Process2
    machine_check.argtypes = [HANDLE, C.POINTER(U16), C.POINTER(U16)]
    machine_check.restype = C.c_int
    process_machine, host_machine = U16(), U16()
    if not machine_check(HANDLE(-1), C.byref(process_machine), C.byref(host_machine)):
        raise error()
    if host_machine.value != 0x8664:
        raise OSError('首版只支持原生 x64 Windows，不支持 ARM64 仿真。')
    signatures = {
        'CreateFileW': ([C.c_wchar_p, U32, U32, HANDLE, U32, U32, HANDLE], HANDLE),
        'CloseHandle': ([HANDLE], C.c_int),
        'GetFileInformationByHandleEx': ([HANDLE, C.c_int, HANDLE, U32], C.c_int),
        'GetVolumeInformationByHandleW': ([HANDLE, C.c_wchar_p, U32, C.POINTER(U32), C.POINTER(U32), C.POINTER(U32), C.c_wchar_p, U32], C.c_int),
        'GetDriveTypeW': ([C.c_wchar_p], U32),
        'GetFinalPathNameByHandleW': ([HANDLE, C.c_wchar_p, U32, U32], U32),
        'LockFileEx': ([HANDLE, U32, U32, U32, U32, C.POINTER(OVERLAPPED)], C.c_int),
        'UnlockFileEx': ([HANDLE, U32, U32, U32, C.POINTER(OVERLAPPED)], C.c_int),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(k, name); fn.argtypes = args; fn.restype = result
    n.NtCreateFile.argtypes = [C.POINTER(HANDLE), U32, C.POINTER(OBJECT_ATTRIBUTES), C.POINTER(IO_STATUS_BLOCK),
                              HANDLE, U32, U32, U32, U32, HANDLE, U32]
    n.NtCreateFile.restype = C.c_int32
    n.NtSetInformationFile.argtypes = [HANDLE, C.POINTER(IO_STATUS_BLOCK), HANDLE, U32, C.c_int]
    n.NtSetInformationFile.restype = C.c_int32
    n.RtlNtStatusToDosError.argtypes = [C.c_int32]; n.RtlNtStatusToDosError.restype = U32
    _api = k, n
    return _api


def error(code=None):
    return C.WinError(C.get_last_error() if code is None else code)


def close(handle):
    if handle:
        api()[0].CloseHandle(handle)


def query(handle, kind, cls):
    out = cls()
    if not api()[0].GetFileInformationByHandleEx(handle, kind, C.byref(out), C.sizeof(out)):
        raise error()
    return out


def info(handle):
    basic, standard, ident = query(handle, 0, BASIC), query(handle, 1, STANDARD), query(handle, 18, ID_INFO)
    attrs = basic.FileAttributes
    if attrs & REPARSE or standard.DeletePending:
        raise OSError('拒绝重解析点或待删除对象。')
    epoch = 116444736000000000
    return SimpleNamespace(st_dev=ident.VolumeSerialNumber,
        st_ino=int.from_bytes(bytes(ident.FileId), 'little'),
        st_size=standard.EndOfFile, st_mtime_ns=(basic.LastWriteTime - epoch) * 100,
        st_mtime=(basic.LastWriteTime - epoch) / 10000000,
        st_ctime_ns=(basic.CreationTime - epoch) * 100,
        st_ctime=(basic.CreationTime - epoch) / 10000000,
        st_mode=(stat.S_IFDIR if standard.Directory else stat.S_IFREG) | 0o600,
        st_nlink=standard.NumberOfLinks, st_file_attributes=attrs)


def check_directory(handle):
    st = info(handle)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError('路径段不是目录。')
    if st.st_file_attributes & (0x1000 | 0x40000 | 0x400000):
        raise OSError('不支持云端或离线目录。')
    if query(handle, 23, U32).value & 1:
        raise OSError('Windows 内测版暂不支持大小写敏感目录。')
    return st


def nt_open(parent, name, *, access=READ_ATTR, share=SHARE_ALL, disposition=1, directory=False):
    component(name)
    buf = C.create_unicode_buffer(name)
    text = UNICODE_STRING(len(name.encode('utf-16-le')), C.sizeof(buf), C.cast(buf, HANDLE))
    attrs = OBJECT_ATTRIBUTES(C.sizeof(OBJECT_ATTRIBUTES), parent, C.pointer(text), 0x40 | 0x1000, None, None)
    status, handle = IO_STATUS_BLOCK(), HANDLE()
    options = 0x00200000 | 0x20 | (0x1 if directory is True else 0x40 if directory is False else 0)
    result = api()[1].NtCreateFile(C.byref(handle), access | SYNC, C.byref(attrs), C.byref(status),
        None, 0x80, share, disposition, options, None, 0)
    if result < 0:
        raise error(api()[1].RtlNtStatusToDosError(result))
    try:
        if directory or stat.S_ISDIR(info(handle.value).st_mode):
            check_directory(handle.value)
        else:
            info(handle.value)
        return handle.value
    except BaseException:
        close(handle.value)
        raise


class Directory:
    def __init__(self, path, handles):
        self.path, self.handles = path, handles

    @property
    def handle(self):
        if not self.handles:
            raise OSError('目录句柄已关闭。')
        return self.handles[-1]

    def close(self):
        handles, self.handles = self.handles, []
        for handle in reversed(handles):
            close(handle)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()


def open_directory(path, *, create=False):
    path = absolute(path)
    root = path[:3].replace('/', '\\')
    k, _ = api()
    if k.GetDriveTypeW(root) not in (2, 3):
        raise OSError('仅支持本地 NTFS 卷。')
    handle = k.CreateFileW('\\\\?\\' + root, READ_ATTR | 1 | SYNC | 0x20000, 3, None, 3, 0x02200000, None)
    if handle == C.c_void_p(-1).value:
        raise error()
    handles = [handle]
    try:
        fs = C.create_unicode_buffer(32)
        if not k.GetVolumeInformationByHandleW(handle, None, 0, None, None, None, fs, len(fs)):
            raise error()
        if fs.value != 'NTFS':
            raise OSError('仅支持本地 NTFS 卷。')
        check_directory(handle)
        for part in path[3:].split('/') if path[3:] else []:
            child = nt_open(handles[-1], part, access=READ_ATTR | 1 | 0x20000, share=3,
                            disposition=3 if create else 1, directory=True)
            handles.append(child)
        final = C.create_unicode_buffer(32768)
        length = k.GetFinalPathNameByHandleW(handles[-1], final, len(final), 0)
        if not length or length >= len(final):
            raise error()
        canonical = final.value.removeprefix('\\\\?\\').replace('\\', '/').rstrip('/')
        # Reject 8.3/SUBST aliases. Case differences alone remain harmless here;
        # identity and boundary checks elsewhere still use held parent handles.
        if canonical.lower() != path.rstrip('/').lower():
            raise OSError('不支持短路径别名或映射盘路径，请选择完整本地路径。')
        return Directory(path, handles)
    except BaseException:
        for item in reversed(handles):
            close(item)
        raise


def names(directory):
    """Enumerate names against the held directory, never a reopened path."""
    buf = C.create_string_buffer(65536)
    first = True
    result = []
    while True:
        if not api()[0].GetFileInformationByHandleEx(directory.handle, 11 if first else 10, buf, len(buf)):
            code = C.get_last_error()
            if code == 18:  # ERROR_NO_MORE_FILES
                return result
            raise error(code)
        first = False
        offset = 0
        while True:
            next_offset = struct.unpack_from('<I', buf, offset)[0]
            length = struct.unpack_from('<I', buf, offset + 60)[0]
            if length % 2 or offset + 104 + length > len(buf):
                raise OSError('目录枚举返回了无效记录。')
            name = bytes(buf[offset + 104:offset + 104 + length]).decode('utf-16-le')
            if name not in ('.', '..'):
                result.append(name)
            if not next_offset:
                break
            if next_offset < 104 or offset + next_offset >= len(buf):
                raise OSError('目录枚举偏移无效。')
            offset += next_offset


def rename(handle, target_directory, name, *, replace=False):
    component(name)
    encoded = name.encode('utf-16-le')
    # Include the complete ABI struct plus trailing name storage/NUL padding.
    length = C.sizeof(RENAME) + len(encoded)
    buffer = C.create_string_buffer(length)
    record = RENAME.from_buffer(buffer)
    record.Flags = int(replace)
    record.RootDirectory = target_directory.handle
    record.FileNameLength = len(encoded)
    C.memmove(C.addressof(buffer) + RENAME.FileName.offset, encoded, len(encoded))
    # Use the native relative-handle API without Win32 path conversion.
    status = IO_STATUS_BLOCK()
    n = api()[1]
    result = n.NtSetInformationFile(handle, C.byref(status), buffer, length, 10)
    if result < 0:
        failure = error(n.RtlNtStatusToDosError(result))
        failure.ntstatus = result & 0xffffffff
        raise failure
    if result != 0:
        raise RuntimeError('重命名返回非完成状态，需恢复检查。')


def lock(handle, *, unlock=False):
    overlapped = OVERLAPPED()
    # Beyond EOF: holder JSON remains readable without touching the locked byte.
    overlapped.Offset = 0x7fffffff
    fn = api()[0].UnlockFileEx if unlock else api()[0].LockFileEx
    ok = fn(handle, 0, 1, 0, C.byref(overlapped)) if unlock else fn(handle, 3, 0, 1, 0, C.byref(overlapped))
    if not ok:
        code = C.get_last_error()
        if not unlock and code in (32, 33, 158):
            raise BlockingIOError('另一个实例正在运行。')
        raise error(code)


def _allowed_writer(sid, owner, allowed):
    # OWNER RIGHTS (S-1-3-4) means the object's actual owner, not everyone.
    return sid in allowed or (sid == 'S-1-3-4' and owner in allowed)


def require_private_directory(directory):
    """Reject state roots writable by principals other than user/admin/SYSTEM.

    Does not change ACLs or request elevation. Inherit-only ACEs do not grant
    access to this directory; descendants are checked separately on creation.
    """
    adv = C.WinDLL('advapi32', use_last_error=True)
    k = api()[0]
    get = adv.GetSecurityInfo
    get.argtypes = [HANDLE, U32, U32, C.POINTER(HANDLE), HANDLE, C.POINTER(HANDLE), HANDLE, C.POINTER(HANDLE)]
    get.restype = U32
    sid_string = adv.ConvertSidToStringSidW
    sid_string.argtypes = [HANDLE, C.POINTER(HANDLE)]; sid_string.restype = C.c_int
    free = k.LocalFree; free.argtypes = [HANDLE]; free.restype = HANDLE
    get_ace = adv.GetAce; get_ace.argtypes = [HANDLE, U32, C.POINTER(HANDLE)]; get_ace.restype = C.c_int
    open_token = adv.OpenProcessToken
    open_token.argtypes = [HANDLE, U32, C.POINTER(HANDLE)]; open_token.restype = C.c_int
    token_info = adv.GetTokenInformation
    token_info.argtypes = [HANDLE, U32, HANDLE, U32, C.POINTER(U32)]; token_info.restype = C.c_int
    def sid_text(sid):
        out = HANDLE()
        if not sid_string(sid, C.byref(out)):
            raise error()
        try:
            return C.wstring_at(out.value)
        finally:
            free(out)
    token = HANDLE()
    if not open_token(HANDLE(-1), 8, C.byref(token)):
        raise error()
    try:
        length = U32()
        token_info(token, 1, None, 0, C.byref(length))
        buf = C.create_string_buffer(length.value)
        if not token_info(token, 1, buf, len(buf), C.byref(length)):
            raise error()
        allowed = {sid_text(HANDLE.from_buffer(buf)), 'S-1-5-18', 'S-1-5-32-544'}
    finally:
        close(token.value)
    owner, dacl, descriptor = HANDLE(), HANDLE(), HANDLE()
    code = get(directory.handle, 1, 1 | 4, C.byref(owner), None, C.byref(dacl), None, C.byref(descriptor))
    if code:
        raise error(code)
    try:
        owner_text = sid_text(owner) if owner.value else None
        if owner_text not in allowed:
            raise PermissionError('状态目录所有者不可信，拒绝写入。')
        if not dacl.value:
            raise PermissionError('状态目录没有有效访问控制，拒绝写入。')
        count = U16.from_address(dacl.value + 4).value
        for i in range(count):
            ace = HANDLE()
            if not get_ace(dacl, i, C.byref(ace)):
                raise error()
            kind, flags, size = struct.unpack('<BBH', C.string_at(ace.value, 4))
            if flags & 8:  # INHERIT_ONLY_ACE
                continue
            if kind not in (0, 1):
                raise PermissionError('状态目录访问控制类型未验证，拒绝写入。')
            if kind == 0 and size >= 12:
                mask = U32.from_address(ace.value + 4).value
                if mask & (0x40000000 | 0x10000000 | 0xD0156) and not _allowed_writer(sid_text(ace.value + 8), owner_text, allowed):
                    raise PermissionError('状态目录可被其他用户写入，请选择个人本地目录。')
    finally:
        free(descriptor)
