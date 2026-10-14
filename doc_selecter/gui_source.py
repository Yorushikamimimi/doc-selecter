"""GUI 主动选择来源的配置准入；不写用户配置、不改 CLI 配置契约。"""
import copy
import os
import stat
import unicodedata

from .config import (
    ConfigError, Settings, TEST_ROOT_ENV, _validate_target_parent_name, check_dir_pair,
    check_protected_paths, check_target_root_containment,
)
from .rules import is_sensitive_dir, is_package_dir, is_code_project_dir


def selected_settings(base: Settings, path: str) -> Settings:
    if not isinstance(path, str) or not os.path.isabs(path) or '\x00' in path:
        raise ConfigError('请选择一个有效的绝对目录。')
    if any(p in {'.', '..'} for p in path.split('/')):
        raise ConfigError('目录不能含 . 或 .. 路径段。')
    source = os.path.normpath(path)
    test_prefix = TEST_ROOT_ENV + '='
    injected = base.source.startswith(test_prefix)
    sandbox = base.source[len(test_prefix):] if injected else os.path.dirname(base.state_dir)
    if base.sandboxed or injected:
        if os.path.commonpath((source, sandbox)) != sandbox:
            raise ConfigError('人工沙盒模式只能选择本沙盒内的人工目录。')
    target_name = _validate_target_parent_name(base.target_parent_name)
    target = os.path.join(source, target_name)
    check_protected_paths(source, base.state_dir, target)
    check_dir_pair(source, base.state_dir)
    check_target_root_containment(source, target_name)
    # 名称筛查先于对应路径段的 metadata；系统 /private 锚点不是私人目录名。
    current = os.sep
    for part in source.split('/')[1:]:
        current = os.path.join(current, part)
        if current != '/private' and (is_sensitive_dir(part) or is_package_dir(part) or part.startswith('.')):
            raise ConfigError('所选路径包含受保护目录，拒绝使用；不展示名称。')
        st = os.lstat(current)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ConfigError('所选目录的路径含链接或非目录，拒绝使用。')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    current = os.sep
    boundary = sandbox if base.sandboxed or injected else os.sep
    try:
        for part in source.split('/')[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            current = os.path.join(current, part)
            if os.path.commonpath((current, boundary)) == boundary:
                with os.scandir(fd) as entries:
                    names = frozenset(e.name for e in entries)
                if is_code_project_dir(names)[0]:
                    raise ConfigError('所选目录属于受保护的成套项目，不能拆分整理。')
        with os.scandir(fd) as entries:
            names = frozenset(e.name for e in entries)
        if is_code_project_dir(names)[0]:
            raise ConfigError('所选目录是受保护的成套项目，不能拆分整理。')
        try:
            target_stat = os.stat(target_name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(target_stat.st_mode):
                raise ConfigError('分类目录是链接或非目录，拒绝使用。')
    finally:
        os.close(fd)
    # 所有原始段均已通过 nofollow 准入；使用其纯字符串 NFC 形式作为新可信锚点。
    # 不在关闭描述符后再调用 Settings.__post_init__ 的 realpath，避免跟随后来替换的链接。
    selected = copy.copy(base)
    object.__setattr__(selected, 'downloads_dir', source)
    object.__setattr__(selected, 'canonical_downloads_dir', unicodedata.normalize('NFC', source))
    return selected
