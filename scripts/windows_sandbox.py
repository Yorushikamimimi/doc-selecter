"""Create a NEW private artificial root; never change an existing directory ACL."""
import ctypes as C
from pathlib import Path
import secrets
import sys
import tempfile


def make_private_root(prefix):
    if sys.platform != 'win32':
        raise RuntimeError('Windows artificial roots require Windows.')
    if not prefix or any(c not in 'abcdefghijklmnopqrstuvwxyz-' for c in prefix):
        raise ValueError('Invalid artificial-root prefix.')
    handle, word = C.c_void_p, C.c_uint32
    kernel = C.WinDLL('kernel32', use_last_error=True)
    adv = C.WinDLL('advapi32', use_last_error=True)
    kernel.CloseHandle.argtypes = [handle]; kernel.CloseHandle.restype = C.c_int
    kernel.LocalFree.argtypes = [handle]; kernel.LocalFree.restype = handle
    adv.OpenProcessToken.argtypes = [handle, word, C.POINTER(handle)]
    adv.OpenProcessToken.restype = C.c_int
    adv.GetTokenInformation.argtypes = [handle, word, handle, word, C.POINTER(word)]
    adv.GetTokenInformation.restype = C.c_int
    adv.ConvertSidToStringSidW.argtypes = [handle, C.POINTER(handle)]
    adv.ConvertSidToStringSidW.restype = C.c_int
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [C.c_wchar_p, word, C.POINTER(handle), C.POINTER(word)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = C.c_int
    token, sid, descriptor = handle(), handle(), handle()
    try:
        if not adv.OpenProcessToken(handle(-1), 8, C.byref(token)):
            raise C.WinError(C.get_last_error())
        length = word()
        adv.GetTokenInformation(token, 1, None, 0, C.byref(length))
        data = C.create_string_buffer(length.value)
        if not adv.GetTokenInformation(token, 1, data, len(data), C.byref(length)):
            raise C.WinError(C.get_last_error())
        if not adv.ConvertSidToStringSidW(handle.from_buffer(data), C.byref(sid)):
            raise C.WinError(C.get_last_error())
        sddl = 'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;' + C.wstring_at(sid.value) + ')'
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(descriptor), None):
            raise C.WinError(C.get_last_error())

        class SecurityAttributes(C.Structure):
            _fields_ = [('length', word), ('descriptor', handle), ('inherit_handle', C.c_int)]

        attributes = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, False)
        create = kernel.CreateDirectoryW
        create.argtypes = [C.c_wchar_p, C.POINTER(SecurityAttributes)]
        create.restype = C.c_int
        # Resolve only the harness's TEMP alias; application admission is unchanged.
        parent = Path(tempfile.gettempdir()).resolve(strict=True)
        for _ in range(100):
            root = parent / (prefix + secrets.token_hex(8))
            if create(str(root), C.byref(attributes)):
                return root
            code = C.get_last_error()
            if code != 183:
                raise C.WinError(code)
        raise FileExistsError('Could not allocate a new artificial root.')
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if sid.value:
            kernel.LocalFree(sid)
        if token.value:
            kernel.CloseHandle(token)
