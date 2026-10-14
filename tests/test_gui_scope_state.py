"""R9/R10：真实 Tk 控件与 App 回调的当前范围/历史计划准入。"""
from __future__ import annotations

from unittest.mock import patch
import tkinter as tk

from tests.harness import SandboxTestCase, require_gui_source, require_gui_preview
from tests.test_gui import _SHARED_ROOT
from doc_selecter import gui
from doc_selecter.atomic_move import lstat_identity
from doc_selecter.config import ConfigError
from doc_selecter.executor import execute_plan


class GuiScopeStateTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        for name in ('showinfo', 'showwarning', 'showerror'):
            self.enterContext(patch.object(gui.messagebox, name))
        self.enterContext(patch.object(gui.messagebox, 'askokcancel', return_value=True))
        self.sandbox.add_file('Chosen/a.pdf', b'ARTIFICIAL CHOSEN')
        self.sandbox.add_file('Other/b.pdf', b'ARTIFICIAL OTHER')

    def _app(self):
        root = tk.Toplevel(_SHARED_ROOT)
        root.withdraw()
        app = gui.App(root, self.sandbox.settings)
        require_gui_source(app, self.sandbox.settings.downloads_dir)
        self.addCleanup(app.on_close)
        return app

    def _choose(self, app, roots):
        app.selected_scope = list(roots)
        app.subfolder_var.set(bool(roots))
        app.on_toggle_subfolders()

    def _identities(self):
        return {name: lstat_identity(str(self.sandbox.downloads / name)).as_dict()
                for name in ('Chosen/a.pdf', 'Other/b.pdf')}

    def _chosen_preview(self):
        app = self._app()
        self._choose(app, ['Chosen'])
        require_gui_preview(app)
        app.on_confirm()
        self.assertEqual(app.confirmed_hash, app.plan['plan_hash'])
        return app

    def _reject_failed_preview(self, function, error):
        app = self._chosen_preview()
        old = app.plan
        identities = self._identities()
        plan_bytes = self.sandbox.plan_file(old['plan_id']).read_bytes()
        confirmation = self.sandbox.state / 'confirmations' / (old['plan_id'] + '.json')
        confirm_bytes = confirmation.read_bytes()
        with patch.object(gui, function, side_effect=error):
            self._choose(app, ['Other'])
        self.assertIs(app.plan, old, '保留历史计划供查看/撤销')
        self.assertIn('Other', app.scope_label['text'])
        self.assertIsNone(app.confirmed_hash)
        self.assertEqual(str(app.confirm_button['state']), 'disabled')
        self.assertEqual(str(app.execute_button['state']), 'disabled')
        with patch.object(gui, 'write_confirmation', wraps=gui.write_confirmation) as writer:
            app.on_confirm()
        self.assertFalse(writer.called, '失败后不得重新写旧范围的确认')
        # 即使旧确认字段被重新赋值，真正执行入口也必须核对当前范围/保存状态。
        app.confirmed_hash = old['plan_hash']
        self.assertIsNone(app.perform_execution())
        app.confirmed_hash = old['plan_hash']
        app.on_execute()
        self.assertEqual(self._identities(), identities)
        self.assertEqual(confirmation.read_bytes(), confirm_bytes)
        self.assertEqual(self.sandbox.plan_file(old['plan_id']).read_bytes(), plan_bytes)

        # 同一个窗口显式重新成功预览后，正常 UI 闭环仍然成立。
        app.on_preview()
        self.assertNotEqual(app.plan['plan_id'], old['plan_id'])
        self.assertEqual(app.plan['selected_scope'], ['', 'Other'])
        app.on_confirm()
        app.on_execute()
        self.assertFalse((self.sandbox.downloads / 'Other/b.pdf').exists())
        self.assertTrue((self.sandbox.downloads / 'Chosen/a.pdf').is_file())
        app.on_undo()
        self.assertEqual(self._identities(), identities)
        self.assertEqual(self.sandbox.plan_file(old['plan_id']).read_bytes(), plan_bytes)

    def test_build_failure_rejects_old_scope_and_retry_roundtrip(self):
        self._reject_failed_preview('build_plan', OSError('ARTIFICIAL build failure'))

    def test_write_failure_rejects_old_scope_and_retry_roundtrip(self):
        self._reject_failed_preview('write_plan', OSError('ARTIFICIAL disk full'))

    def test_config_check_failure_rejects_old_scope_and_retry_roundtrip(self):
        self._reject_failed_preview('assert_downloads_exists', ConfigError('ARTIFICIAL configuration failure'))

    def test_failure_returning_to_old_scope_still_requires_successful_preview(self):
        app = self._chosen_preview()
        old_id = app.plan['plan_id']
        with patch.object(gui, 'write_plan', side_effect=OSError('ARTIFICIAL disk full')):
            self._choose(app, ['Other'])
        app.selected_scope = ['Chosen']  # 不能凭恢复旧选择撤销失败失效状态
        with patch.object(gui, 'write_confirmation', wraps=gui.write_confirmation) as writer:
            app.on_confirm()
        self.assertFalse(writer.called)
        self.assertEqual(app.plan['plan_id'], old_id)
        self.assertIsNone(app.perform_execution())
        self.assertTrue((self.sandbox.downloads / 'Chosen/a.pdf').is_file())

    def test_confirm_rejects_scope_changed_without_preview_callback(self):
        app = self._chosen_preview()
        app.selected_scope = ['Other']
        with patch.object(gui, 'write_confirmation', wraps=gui.write_confirmation) as writer:
            app.on_confirm()
        self.assertFalse(writer.called)
        self.assertIsNone(app.confirmed_hash)
        self.assertIsNone(app.perform_execution())
        self.assertTrue((self.sandbox.downloads / 'Chosen/a.pdf').is_file())

    def test_actual_execution_rejects_changed_recursive_switch(self):
        app = self._chosen_preview()
        app.subfolder_var.set(False)
        app.include_subfolders = False  # 未走刷新回调也不能绕过实际入口
        self.assertIsNone(app.perform_execution())
        self.assertTrue((self.sandbox.downloads / 'Chosen/a.pdf').is_file())

    def test_actual_execution_rejects_widget_switch_changed_without_callback(self):
        app = self._chosen_preview()
        app.subfolder_var.set(False)
        self.assertIsNone(app.perform_execution())
        self.assertTrue((self.sandbox.downloads / 'Chosen/a.pdf').is_file())

    def test_actual_execution_rejects_changed_category_collection(self):
        app = self._chosen_preview()
        app._selection_vars['文档资料'].set(False)
        self.assertIsNone(app.perform_execution())
        self.assertTrue((self.sandbox.downloads / 'Chosen/a.pdf').is_file())

    def test_actual_execution_rechecks_scope_after_modal_confirmation(self):
        app = self._chosen_preview()
        def change_scope(*args, **kwargs):
            app.selected_scope = ['Other']
            return True
        with patch.object(gui.messagebox, 'askokcancel', side_effect=change_scope):
            app.on_execute()
        self.assertTrue((self.sandbox.downloads / 'Chosen/a.pdf').is_file())
        self.assertIsNone(app.confirmed_hash)

    def test_failed_scope_preview_preserves_executed_plan_undo(self):
        identities = self._identities()
        app = self._chosen_preview()
        app.on_execute()
        old = app.plan
        self.assertFalse((self.sandbox.downloads / 'Chosen/a.pdf').exists())
        with patch.object(gui, 'write_plan', side_effect=OSError('ARTIFICIAL disk full')):
            self._choose(app, ['Other'])
        self.assertIs(app.plan, old)
        self.assertEqual(str(app.undo_button['state']), 'normal')
        self.assertEqual(str(app.confirm_button['state']), 'disabled')
        app.on_confirm()
        self.assertIsNone(app.confirmed_hash)
        app.on_undo()
        self.assertEqual(self._identities(), identities)

    def _reopen_roundtrip(self, roots, *, recursive):
        p = self.sandbox.make_plan(scope_dirs=roots, recursive=recursive, confirm=False)
        plan_bytes = self.sandbox.plan_file(p['plan_id']).read_bytes()
        app = self._app()
        self.assertEqual(app.plan['plan_id'], p['plan_id'])
        self.assertIsNone(app.confirmed_hash, '加载历史展示不是自动确认')
        self.assertFalse((self.sandbox.state / 'confirmations' / (p['plan_id'] + '.json')).exists())
        self.assertFalse(app.include_subfolders)
        self.assertFalse(app.subfolder_var.get())
        self.assertEqual(app.selected_scope, [])
        self.assertEqual(app.plan['selected_scope'], ['', *roots])
        self.assertEqual(app.plan['recursive'], recursive)
        self.assertEqual(str(app.confirm_button['state']), 'disabled')
        if recursive:
            app.selected_scope = list(roots)  # 用户为新操作显式重选；不借历史展开范围授权。
            app.subfolder_var.set(True)
            app.on_toggle_subfolders()
        app.on_preview()  # 选择来源后必须重新成功预览，历史不授予执行资格。
        self.assertEqual(str(app.confirm_button['state']), 'normal')
        self.assertEqual(str(app.execute_button['state']), 'disabled')
        before = {op['source']: lstat_identity(op['source']).as_dict() for op in p['operations']}
        app.on_confirm()
        app.on_execute()
        self.assertTrue(all(not lstat_identity(path) for path in before))
        app.on_undo()
        self.assertEqual({path: lstat_identity(path).as_dict() for path in before}, before)
        self.assertEqual(self.sandbox.plan_file(p['plan_id']).read_bytes(), plan_bytes)
        return app

    def test_reopen_recursive_preview_restores_explicit_scope_and_controls(self):
        app = self._reopen_roundtrip(['Chosen'], recursive=True)
        self.assertIn('Chosen', app.scope_label['text'])
        self.assertEqual(str(app.pick_button['state']), 'normal')
        self.assertTrue((self.sandbox.downloads / 'Other/b.pdf').is_file())

    def test_reopen_deep_selection_never_restores_traversal_ancestors(self):
        for name in ('A/chosen/a.pdf', 'A/chosen/Sub/b.pdf', 'A/root.pdf', 'A/sibling/c.pdf'):
            self.sandbox.add_file(name)
        app = self._reopen_roundtrip(['A/chosen'], recursive=True)
        self.assertNotIn('A', app.selected_scope)
        self.assertNotIn('A/chosen/Sub', app.selected_scope, '不能拿展开 scan_scope 恢复显式根')
        self.assertTrue((self.sandbox.downloads / 'A/root.pdf').is_file())
        self.assertTrue((self.sandbox.downloads / 'A/sibling/c.pdf').is_file())
        self.assertEqual({op['rel_path'] for op in app.plan['operations']},
                         {'A/chosen/a.pdf', 'A/chosen/Sub/b.pdf'})

    def test_reopen_top_level_preview_keeps_controls_top_level(self):
        self.sandbox.add_file('top.pdf')
        app = self._reopen_roundtrip([], recursive=False)
        self.assertEqual(app.scope_label['text'], '扫描范围：仅顶层')
        self.assertEqual(str(app.pick_button['state']), 'disabled')
        self.assertTrue((self.sandbox.downloads / 'Chosen/a.pdf').is_file())

    def test_reopen_executed_history_preserves_real_undo_entry(self):
        identities = self._identities()
        p = self.sandbox.make_plan(scope_dirs=['Chosen'], recursive=True)
        plan_bytes = self.sandbox.plan_file(p['plan_id']).read_bytes()
        confirmation = self.sandbox.state / 'confirmations' / (p['plan_id'] + '.json')
        confirm_bytes = confirmation.read_bytes()
        self.assertEqual(execute_plan(self.sandbox.settings, p['plan_id'], p['plan_hash']).moved, 1)
        app = self._app()
        self.assertIsNone(app.confirmed_hash)
        self.assertEqual(app.selected_scope, [])
        self.assertEqual(app.plan['selected_scope'], ['', 'Chosen'])
        self.assertEqual(str(app.undo_button['state']), 'normal')
        app.on_undo()
        self.assertEqual(self._identities(), identities)
        self.assertEqual(self.sandbox.plan_file(p['plan_id']).read_bytes(), plan_bytes)
        self.assertEqual(confirmation.read_bytes(), confirm_bytes)
