"""Pure, conservative Windows path policy (no filesystem access)."""
import ntpath
import re
import unicodedata

PLATFORM_ID = 'windows-ntfs-v1'
ARCHIVE_UNAVAILABLE = 'Windows 内测版暂不支持空目录归档和剩余项归档。'
RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$', re.I)
CLOUD_DIRECTORY = re.compile(r'^(OneDrive(?: - .+)?|Dropbox|Google Drive|GoogleDrive|iCloudDrive|Box)$', re.I)


def component(name):
    if (not isinstance(name, str) or name in ('', '.', '..') or
            name[-1:] in (' ', '.') or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name) or
            RESERVED.fullmatch(name.split('.')[0].rstrip(' ')) or
            unicodedata.normalize('NFC', name) != name or
            len(name.encode('utf-16-le')) // 2 > 255):
        raise ValueError('Windows 文件名不受支持；不改名、不移动。')
    return name


def absolute(path):
    """Accept drive-absolute paths only; reject ambiguity *before* normalization."""
    if not isinstance(path, str) or not path:
        raise ValueError('请选择本地 NTFS 绝对路径。')
    raw = path.replace('\\', '/')
    if not re.match(r'^[A-Za-z]:/', raw) or raw.startswith('//'):
        raise ValueError('不支持网络、设备或相对路径。')
    parts = raw[3:].split('/')
    if parts and parts[-1] == '':
        parts.pop()
    for part in parts:
        component(part)
        if CLOUD_DIRECTORY.fullmatch(part):
            raise ValueError('Windows 内测版不支持已知云同步目录。')
    out = raw[0].upper() + ':/' + '/'.join(parts)
    # Deliberate first-release limit, including paths after classification.
    if len(out.encode('utf-16-le')) // 2 >= 248:
        raise ValueError('Windows 内测版暂不支持此长度的路径。')
    return out


def under(child, parent):
    child, parent = absolute(child), absolute(parent)
    # Used for rejecting protected/overlapping roots; conservative on case aliases.
    c, p = ntpath.normcase(child), ntpath.normcase(parent).rstrip('\\')
    return c == p or c.startswith(p + '\\')


def unsupported_name(name):
    try:
        component(name)
    except (ValueError, UnicodeError):
        return True
    return name.lower().endswith(('.lnk', '.url'))
