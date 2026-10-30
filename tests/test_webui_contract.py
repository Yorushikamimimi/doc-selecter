"""Headless bridge/resource checks. Never import or start a native webview."""
import ast
from pathlib import Path
import sys
from unittest.mock import Mock
from tests.harness import SandboxTestCase
from doc_selecter.ui_controller import Controller
from doc_selecter.webui import API_METHODS, RESOURCES, api_functions

ROOT = Path(__file__).resolve().parents[1]


class BridgeContractTests(SandboxTestCase):
    def test_only_named_methods_are_exported_and_errors_are_json_views(self):
        c = Controller(self.sandbox.settings)
        self.addCleanup(c.close)
        functions = {f.__name__: f for f in api_functions(c, lambda: {'cancelled': True}, lambda: {'opened': True})}
        self.assertEqual(set(functions), set(API_METHODS) | {'pick_folder', 'pick_subfolders', 'open_target', 'settings_get'})
        self.assertNotIn('select_source', functions)
        self.assertNotIn('_execute', functions)
        self.assertFalse(functions['organize']('anything', True)['ok'])
        self.assertEqual(functions['pick_folder'](), {'ok': True, 'value': {'cancelled': True}})
        self.assertEqual(list((self.sandbox.state/'confirmations').glob('*.json')), [])

    def test_controller_imports_no_widget_or_browser(self):
        tree = ast.parse((ROOT/'doc_selecter/ui_controller.py').read_text())
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        self.assertFalse(any(n and n.split('.')[0] in {'tkinter', 'webview'} for n in imports))

    def test_local_page_does_not_interpolate_untrusted_markup(self):
        root = ROOT/'doc_selecter/webui'
        self.assertEqual({p.name for p in root.iterdir()},set(RESOURCES))
        js = (root/'app.js').read_text(); html=(root/'index.html').read_text()
        for unsafe in ('innerHTML','outerHTML','insertAdjacentHTML','document.write','eval('):
            self.assertNotIn(unsafe, js)
        self.assertIn('textContent',js)
        self.assertIn("default-src 'none'",html)
        self.assertIn("connect-src 'none'",html)
        self.assertNotIn('https://',html);self.assertNotIn('http://',html)
        self.assertIn('prefers-reduced-motion', (root/'app.css').read_text())
        self.assertIn('prefers-color-scheme:dark', (root/'app.css').read_text())

    def test_api_does_not_accept_a_source_path_from_javascript(self):
        c=Controller(self.sandbox.settings); self.addCleanup(c.close)
        picker=Mock(return_value={'cancelled':True})
        api={f.__name__:f for f in api_functions(c,picker,lambda:None)}
        picker.side_effect=lambda: {'cancelled':True}
        self.assertFalse(api['pick_folder']('/artificial/path')['ok'])
        self.assertFalse(c.selected)

    def test_default_app_dispatch_uses_web_and_keeps_instance_lock(self):
        from doc_selecter import app_startup
        from unittest.mock import patch
        import os
        with patch.dict(os.environ, {'DOCSELECTER_TEST_ROOT':str(self.sandbox.root), 'DOCSELECTER_UI':'web'}), patch('doc_selecter.webui.run') as run:
            def inspect(settings):
                with self.assertRaises(BlockingIOError): app_startup.AppInstance(settings.state_dir)
            run.side_effect=inspect
            self.assertEqual(app_startup.app_main([]),0)
            run.assert_called_once()
        instance=app_startup.AppInstance(str(self.sandbox.state));instance.close()

    def test_tk_fallback_dispatch_is_retained_without_opening_window(self):
        from doc_selecter import app_startup
        from unittest.mock import patch
        import os
        root=Mock()
        with patch.dict(os.environ, {'DOCSELECTER_TEST_ROOT':str(self.sandbox.root)}), patch('doc_selecter.webui.run') as web, patch('doc_selecter.gui.build_app',return_value=(root,Mock())) as tk:
            self.assertEqual(app_startup.app_main(['--tk']),0)
            tk.assert_called_once();web.assert_not_called();root.mainloop.assert_called_once()

    def test_close_waits_for_bridge_replies_before_native_teardown(self):
        import threading
        from doc_selecter.webui import close_handler
        window=Mock(); controller=Mock(); controller.close.return_value=True
        window.evaluate_js.side_effect=[False,True]
        destroyed=threading.Event();window.destroy.side_effect=destroyed.set
        closing=close_handler(window,controller,threading.Lock(),threading.Event())
        self.assertFalse(closing())
        self.assertTrue(destroyed.wait(2))
        self.assertTrue(closing())
        self.assertEqual(window.evaluate_js.call_count,2)

    def test_close_veto_during_move_or_folder_dialog_never_tears_down(self):
        import threading
        from doc_selecter.webui import close_handler
        window=Mock();controller=Mock();controller.close.return_value=False
        lock=threading.Lock();closing=close_handler(window,controller,lock,threading.Event())
        self.assertFalse(closing());window.destroy.assert_not_called()
        lock.acquire();controller.close.return_value=True
        self.assertFalse(closing());window.evaluate_js.assert_not_called();lock.release()
