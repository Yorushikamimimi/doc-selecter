"""Local WebKit view. Only the flat, explicitly registered API reaches Python."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading
import time

from .ui_controller import Controller, RequestRefused

API_METHODS = (
    'get_state', 'preview', 'set_categories', 'set_filter', 'set_subfolders',
    'organize', 'undo', 'get_details', 'get_history', 'return_to', 'recover_check',
    'settings_set', 'check_new_files', 'remaining_discover', 'remaining_select',
    'remaining_preview', 'remaining_cancel', 'remaining_organize',
)
RESOURCES = ('index.html', 'app.css', 'app.js')


def api_functions(controller, pick_folder, open_target):
    """Return a flat registry, never a traversable object containing the controller.

    pywebview 6 resolves dotted js_api attributes; using its expose registry with
    js_api=None prevents access to controller internals through that mechanism.
    """
    methods = {name: getattr(controller, name) for name in API_METHODS}
    methods.update(pick_folder=pick_folder, open_target=open_target,
                   pick_subfolders=controller.list_folders,
                   settings_get=lambda: controller.get_state()['settings'])
    result = []
    for name, method in methods.items():
        def wrapped(*args, _method=method):
            try:
                value = _method(*args)
                json.dumps(value)  # Fail closed on an accidentally exposed object.
                return {'ok': True, 'value': value}
            except Exception as exc:
                return {'ok': False, 'error': controller._public(exc)}
        wrapped.__name__ = name
        result.append(wrapped)
    return result


def close_handler(window, controller, dialog_lock, stopped):
    """Drain bridge replies before Cocoa tears down its main loop.

    A poll reply blocked in WebKit after teardown otherwise keeps a non-daemon
    pywebview bridge thread alive. Moving/undoing still vetoes closing entirely.
    """
    ready = threading.Event()
    started = threading.Event()
    def closing():
        if ready.is_set():
            return True
        if dialog_lock.locked() or not controller.close():
            return False
        if not started.is_set():
            started.set()
            def drain():
                stopped.set()
                try:
                    while not window.evaluate_js('window.prepareClose ? window.prepareClose() : true'):
                        time.sleep(.01)
                    ready.set()
                    window.destroy()
                except Exception:
                    # Keep the window and lifetime lock if shutdown cannot be verified.
                    started.clear()
            threading.Thread(target=drain, name='doc-selecter-ui-close', daemon=True).start()
        return False
    return closing


def run(settings):
    import webview
    controller = Controller(settings)
    assets = Path(__file__).with_name('webui')
    for name in RESOURCES:
        path = assets / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('应用界面资源不完整。')
    webview.settings.update(ALLOW_DOWNLOADS=False, ALLOW_FILE_URLS=False,
                            OPEN_EXTERNAL_LINKS_IN_BROWSER=False)
    window = webview.create_window('文件整理助手', str(assets / 'index.html'),
                                  width=1100, height=760, min_size=(900, 720),
                                  js_api=None, text_select=True)
    dialog_lock = threading.Lock()

    def pick_folder():
        if not dialog_lock.acquire(blocking=False):
            raise RequestRefused('文件夹选择窗口已经打开。')
        try:
            if controller.get_state()['busy']:
                raise RequestRefused('正在处理，请等待完成。')
            paths = window.create_file_dialog(webview.FileDialog.FOLDER, directory=controller.settings.downloads_dir, allow_multiple=False)
            return controller.select_source(paths[0]) if paths else {'cancelled': True}
        finally:
            dialog_lock.release()

    def open_target():
        path = controller.target_to_open()
        subprocess.Popen(['/usr/bin/open', path], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'opened': True}

    window.expose(*api_functions(controller, pick_folder, open_target))
    stopped = threading.Event()
    window.events.closing += close_handler(window, controller, dialog_lock, stopped)
    window.events.closed += lambda: stopped.set()

    def background():
        # The watcher itself enforces settling and scan scope. No automatic moves.
        while not stopped.wait(30):
            state = controller.get_state()
            if state['selected'] and state['settings']['auto_check'] and not state['busy']:
                try:
                    controller.check_new_files()
                except RequestRefused:
                    pass
    # private_mode avoids persisting website data in a user's profile.
    webview.start(background, private_mode=True, debug=False)
    stopped.set()
