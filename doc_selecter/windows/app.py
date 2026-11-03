"""Windows shell integration. Never installs runtimes or changes system policy."""
import ctypes as C
import os
import uuid

WEBVIEW_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/'


def local_app_data():
    # Ask Windows, rather than accepting an environment-supplied trust root.
    folder = (C.c_ubyte * 16).from_buffer_copy(uuid.UUID('f1b32785-6fba-4fcf-9d55-7b8e7f157091').bytes_le)
    value = C.c_void_p()
    shell = C.WinDLL('shell32')
    fn = shell.SHGetKnownFolderPath
    fn.argtypes = [C.c_void_p, C.c_uint32, C.c_void_p, C.POINTER(C.c_void_p)]
    fn.restype = C.c_int32
    if fn(C.byref(folder), 0, None, C.byref(value)) < 0:
        raise OSError('无法定位本机应用数据目录。')
    try:
        return C.wstring_at(value.value).replace('\\', '/')
    finally:
        free = C.WinDLL('ole32').CoTaskMemFree
        free.argtypes = [C.c_void_p]; free.restype = None
        free(value)


def notice(message, *, question=False):
    fn = C.WinDLL('user32', use_last_error=True).MessageBoxW
    fn.argtypes = [C.c_void_p, C.c_wchar_p, C.c_wchar_p, C.c_uint32]
    fn.restype = C.c_int
    return fn(None, message, '文件整理助手', 0x24 if question else 0x10)


def webview_available():
    """Detect Evergreen installation without importing a GUI or launching Edge."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full') as net:
            if winreg.QueryValueEx(net, 'Release')[0] < 394802:
                return False
    except (OSError, TypeError):
        return False
    key = r'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as entry:
                    version = winreg.QueryValueEx(entry, 'pv')[0]
                    if isinstance(version, str) and tuple(int(x) for x in version.split('.')) >= (86, 0, 622, 0):
                        return True
            except (OSError, ValueError):
                pass
    return False


def choose_webview():
    if webview_available():
        return True
    result = notice('网页界面需要 Microsoft WebView2 Runtime 和 .NET Framework 4.6.2 或更新版。\n'
        '请从微软官网自行安装后重新打开：\n' + WEBVIEW_URL +
        '\n\n现在使用 Tk 备用界面？（选择“否”退出）', question=True)
    if result == 6:
        return False
    raise SystemExit(0)


def open_folder(path):
    from .io import pinned_directory
    with pinned_directory(path):
        os.startfile(path.replace('/', '\\'), 'explore')
