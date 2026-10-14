"""Local macOS App entry; CLI config and all file-operation contracts stay intact."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

from .config import (ConfigError, Settings, TEST_ROOT_ENV, load_settings,
                     check_dir_pair, check_protected_paths, check_target_root_containment,
                     DEFAULT_TARGET_PARENT_NAME)

APP_NAME = '文件整理助手'
SUPPORT_FOLDER = '文件整理助手'


def _directory(path: str, *, create: bool) -> None:
    """Open every raw component without following links; only create our directories."""
    if not os.path.isabs(path) or any(p in ('.', '..') for p in path.split('/')) or '\x00' in path:
        raise ConfigError('应用工作目录无效。')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for part in (p for p in path.split('/') if p):
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


def application_paths(home: str) -> tuple[str, str]:
    """Pure path construction; no user source such as Downloads is a startup default."""
    support = os.path.join(home, 'Library', 'Application Support', SUPPORT_FOLDER)
    return os.path.join(support, '未选择来源'), os.path.join(support, 'state')


def load_app_settings(env=None, *, _home=None) -> Settings:
    env = os.environ if env is None else env
    if env.get(TEST_ROOT_ENV):
        root = os.path.normpath(os.path.abspath(os.path.expanduser(env[TEST_ROOT_ENV])))
        _directory(root, create=False)
        _directory(os.path.join(root, 'Downloads'), create=False)
        _directory(os.path.join(root, 'state'), create=True)
        return load_settings(env=env)  # Config file never inspected in this branch.
    home = _home if _home is not None else os.path.expanduser('~')
    _directory(home, create=False)
    source, state = application_paths(home)
    check_protected_paths(source, state, os.path.join(source, DEFAULT_TARGET_PARENT_NAME))
    check_dir_pair(source, state)
    check_target_root_containment(source, DEFAULT_TARGET_PARENT_NAME)
    _directory(source, create=True)
    _directory(state, create=True)
    # This placeholder is empty and App._source_selected stays False. The first
    # real source still goes through selected_settings's existing trusted gate.
    return Settings(downloads_dir=source, state_dir=state,
                    target_parent_name=DEFAULT_TARGET_PARENT_NAME,
                    sandboxed=False, source='macOS App安全启动默认（来源尚未选择）')


def diagnostic_path(env=None) -> str:
    env = os.environ if env is None else env
    if env.get(TEST_ROOT_ENV):
        state = os.path.join(os.path.normpath(os.path.abspath(env[TEST_ROOT_ENV])), 'state')
    else:
        _, state = application_paths(os.path.expanduser('~'))
    _directory(state, create=True)
    return os.path.join(state, 'app-launch.log')


def _write_diagnostic(message: str, env=None) -> str | None:
    try:
        path = diagnostic_path(env)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            f.write(message + '\n'); f.flush(); os.fsync(f.fileno())
        return path
    except OSError:
        return None


def _failure_notice(message: str, path: str | None) -> None:
    """Native failure notice, passed as arguments, never interpolated into code."""
    script = ('on run argv\n'
              'display alert "文件整理助手无法启动" message (item 1 of argv) '
              'as critical buttons {"好"} default button "好"\nend run')
    detail = message + ('\n诊断记录：' + path if path else '\n诊断记录无法写入；请检查应用支持目录权限。')
    try:
        subprocess.run(['/usr/bin/osascript', '-e', script, '--', detail],
                       check=False, timeout=120, capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        pass


class AppInstance:
    """Separate lifetime lock; never repurpose the core execution/undo lock."""
    def __init__(self, state: str):
        _directory(state, create=False)
        self.fd = os.open(os.path.join(state, 'app-instance.lock'),
                          os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd); self.fd = None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd); self.fd = None


def app_main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    diagnose = '--diagnose' in argv
    # A failed LaunchServices env handoff must never enter real mode during tests.
    if diagnose and not os.environ.get(TEST_ROOT_ENV):
        print('诊断模式必须明确注入 DOCSELECTER_TEST_ROOT；未启动应用。', file=sys.stderr)
        return 64
    instance = None
    try:
        settings = load_app_settings()
        log = diagnostic_path()
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            # LaunchServices may discard a terminal's stderr; keep a local record.
            from contextlib import redirect_stdout, redirect_stderr
            with redirect_stdout(stream), redirect_stderr(stream):
                try:
                    instance = AppInstance(settings.state_dir)
                except BlockingIOError:
                    print('已有文件整理助手窗口运行，未创建第二个窗口。', flush=True)
                    return 0
                from .gui import build_app
                root, app = build_app(settings)
                root.tk.call('tk', 'appname', APP_NAME)
                if diagnose:
                    root.withdraw()  # Before mainloop/idle mapping: real hidden Tk.
                    def observed():
                        report = {'diagnose': True, 'PID': os.getpid(), 'mainloop_reached': True,
                            'title': root.title(), 'tk_appname': root.tk.call('tk', 'appname'),
                            'mapped': bool(root.winfo_ismapped()), 'viewable': bool(root.winfo_viewable()),
                            'source_selected': app._source_selected, 'plan': app.plan,
                            'auto_check': bool(app._watch_enabled_var.get()),
                            'settings': settings.as_public_dict(), 'package_file': __file__,
                            'loaded_package_files': {k: getattr(v, '__file__', None) for k, v in sys.modules.items() if k.startswith('doc_selecter')},
                            'sys_path': sys.path, 'executable': sys.executable}
                        print(json.dumps(report, ensure_ascii=False), flush=True)
                        app.on_close()
                    root.after(30, observed)
                root.mainloop()
        return 0
    except Exception as exc:
        message = '启动所需的 Python／Tk 或应用工作目录不可用，请检查本机运行环境。'
        path = _write_diagnostic(message + '\n' + traceback.format_exc())
        print(message + '\n' + str(exc), file=sys.stderr)
        if not diagnose:
            _failure_notice(message, path)
        return 1
    finally:
        if instance is not None:
            instance.close()
