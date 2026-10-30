"""Real Tk controls and public callbacks. Only native modal answers are stubbed.

This is NOT a human/native-dialog acceptance. Each test destroys its own window.
"""
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from tests.harness import SandboxTestCase, require_gui_source, require_gui_preview
from tests import test_gui as gui_tests
from doc_selecter import gui
from doc_selecter.atomic_move import lstat_identity, dir_identity
from doc_selecter.journal import Journal
from doc_selecter.planner import confirmation_path
from doc_selecter.remaining_archive import build_remaining_plan
from doc_selecter.executor import execute_plan

EVIDENCE = []


class RemainingGuiTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(gui_tests.DISPLAY_AVAILABLE, gui_tests._DISPLAY_ERROR)
        self.calls = []
        for name in ('showerror', 'showwarning', 'showinfo'):
            p = patch.object(gui.messagebox, name, side_effect=lambda *a, **k: self.calls.append(a))
            p.start(); self.addCleanup(p.stop)
        p = patch.object(gui.messagebox, 'askokcancel', return_value=True)
        self.ask = p.start(); self.addCleanup(p.stop)
        self.app = None

    def build(self, selected=True):
        root = gui.tk.Toplevel(gui_tests._SHARED_ROOT)
        self.app = gui.App(root, self.sandbox.settings)
        self.addCleanup(self.app.on_close)
        root.deiconify(); root.update()
        if selected: require_gui_source(self.app, str(self.sandbox.downloads))
        return self.app

    def file(self, name):
        return self.sandbox.add_file(name, b'ARTIFICIAL:' + name.encode())

    def open(self):
        app = self.app or self.build()
        app.remaining_button.invoke(); app.root.update()
        self.assertIsNotNone(app._remaining_dialog, self.calls)
        self.wait_preparation(app._remaining_dialog)
        return app._remaining_dialog

    def wait_preparation(self, d):
        # Async preparation changes timing only; retain every existing assertion.
        deadline = time.monotonic() + 5
        while d._task is not None and time.monotonic() < deadline:
            self.app.root.update()
            time.sleep(0.002)
        self.assertIsNone(d._task, ('preparation timed out', d.status['text'], self.calls))

    def preview(self, d):
        self.wait_preparation(d)
        d.all_button.invoke(); d.preview_button.invoke()
        self.wait_preparation(d)
        self.assertIsNotNone(d.plan, (d.status['text'], self.calls))
        return d.plan

    def restored(self, p):
        for op in p['operations']:
            self.assertIdentityMatches(op['source'], op['identity'])
            self.assertFileContent(op['source'], b'ARTIFICIAL:' + op['rel_path'].encode())
            self.assertNotExists(op['dest'])
        for op in p['directory_operations']:
            self.assertEqual(list(dir_identity(op['source'])), [op['identity']['st_dev'], op['identity']['st_ino']])
            self.assertEqual(os.listdir(op['source']), []); self.assertNotExists(op['dest'])
        logs = {k: Journal(self.app.settings.state_dir, p['plan_id'], k).classify().classification for k in ('execution', 'undo')}
        self.assertEqual(logs, {'execution': 'healthy', 'undo': 'healthy'})
        EVIDENCE.append(dict(route=self.id(), root=str(self.sandbox.root), plan_id=p['plan_id'],
            real_tk=True, modal_answers='stubbed', restored_identity_and_bytes=True, logs=logs))

    def test_no_source_entry_disabled_no_implicit_discovery(self):
        app = self.build(selected=False)
        with patch('doc_selecter.gui_remaining.discover_remaining', side_effect=AssertionError('must not scan')):
            app.remaining_button.invoke(); app.on_remaining_archive()
        self.assertIsNone(app._remaining_dialog)
        self.assertEqual(str(app.remaining_button['state']), 'disabled')

    def test_default_none_all_none_and_close_no_file_operation(self):
        self.file('a.pdf'); self.sandbox.add_dir('empty')
        d = self.open(); self.assertEqual(d.selection(), [])
        self.assertEqual(str(d.confirm_button['state']), 'disabled')
        d.all_button.invoke(); self.assertEqual(d.selection(), ['a.pdf', 'empty'])
        d.none_button.invoke(); self.assertEqual(d.selection(), [])
        self.assertEqual(str(d.preview_button['state']), 'disabled')
        d.close_button.invoke(); self.app.root.update()
        self.assertFalse(d.window.winfo_exists())
        self.assertExists(self.sandbox.downloads/'a.pdf'); self.assertNotExists(self.sandbox.downloads/'下载分类')

    def test_cancel_final_confirm_has_no_confirmation_or_moves_then_roundtrip(self):
        self.file('a.pdf'); self.sandbox.add_dir('empty')
        d = self.open(); p = self.preview(d)
        self.ask.return_value = False; d.confirm_button.invoke()
        self.assertNotExists(confirmation_path(self.app.settings, p['plan_id']))
        self.assertExists(p['operations'][0]['source']); self.assertExists(p['directory_operations'][0]['source'])
        self.assertEqual(self.app.session.head, 0)
        self.ask.return_value = True; d.confirm_button.invoke()
        self.assertEqual((self.app.session.head, self.app.session.active[-1].archived_dirs), (1, 1))
        self.assertIn('已归档 1 个文件、1 个空目录', self.app.daily_status_label['text'])
        self.assertEqual(str(self.app.organize_button['state']), 'disabled')
        self.app.undo_button.invoke(); self.restored(p)
        self.assertIn('已还原 1 个文件、1 个原目录到原位置', self.app.daily_status_label['text'])
        self.assertNotIn('V0', self.app.daily_status_label['text'])

    def test_directory_only_confirm_undo_cancel_then_undo(self):
        self.sandbox.add_dir('empty'); d = self.open(); p = self.preview(d)
        d.confirm_button.invoke(); self.assertIn('1 个空目录', self.app.daily_status_label['text'])
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.ask.return_value = False; self.app.undo_button.invoke()
        self.assertExists(p['directory_operations'][0]['dest']); self.assertEqual(self.app.session.head, 1)
        self.ask.return_value = True; self.app.undo_button.invoke(); self.restored(p)
        self.assertEqual(self.app.session.head, 0); self.assertIn('1 个原目录', self.app.daily_status_label['text'])

    def test_deep_scope_preview_confirmation_is_explicit(self):
        self.file('parent/deep/说明.pdf'); self.file('parent/other/keep.pdf')
        self.sandbox.add_dir('parent/deep/empty')
        app = self.build(); app.include_subfolders = True; app.subfolder_var.set(True)
        app.selected_scope = ['parent/deep']; app._on_scope_changed()
        d = self.open(); p = self.preview(d)
        self.assertEqual(p['remaining_selection'], ['parent/deep/empty', 'parent/deep/说明.pdf'])
        d.confirm_button.invoke(); prompt = self.ask.call_args.args[1]
        self.assertIn('parent/deep', prompt); self.assertIn('1 个原本空目录', prompt)
        self.app.undo_button.invoke(); self.restored(p)
        self.assertExists(self.sandbox.downloads/'parent/other/keep.pdf')

    def test_selection_change_invalidates_saved_preview(self):
        self.file('a.pdf'); self.file('b.pdf'); d = self.open(); p = self.preview(d)
        d.variables['b.pdf'].set(False)
        self.assertIsNone(d.plan); self.assertEqual(str(d.confirm_button['state']), 'disabled')
        d.confirm(); self.assertNotExists(confirmation_path(self.app.settings, p['plan_id']))
        self.assertExists(p['operations'][0]['source'])

    def test_source_change_invalidates_old_archive_dialog(self):
        self.file('a.pdf'); d = self.open(); p = self.preview(d)
        other = self.sandbox.root/'Other'; other.mkdir(); (other/'a.pdf').write_bytes(b'OTHER')
        require_gui_source(self.app, str(other)); d.confirm_button.invoke()
        self.assertIsNone(d.plan); self.assertNotExists(confirmation_path(d.settings, p['plan_id']))
        self.assertExists(p['operations'][0]['source']); self.assertFileContent(other/'a.pdf', b'OTHER')
        d.refresh_button.invoke(); self.assertEqual(d.selection(), [])
        self.wait_preparation(d)
        self.assertEqual(d.settings.downloads_dir, str(other))

    def test_setting_change_during_native_confirmation_blocks_move(self):
        self.file('a.pdf'); d = self.open(); p = self.preview(d)
        def changed(*a, **kw):
            self.app.archive_var.set(True); return True
        self.ask.side_effect = changed; d.confirm_button.invoke()
        self.assertNotExists(confirmation_path(d.settings, p['plan_id']))
        self.assertExists(p['operations'][0]['source']); self.assertEqual(self.app.session.head, 0)
        self.assertFalse(self.app._operation_busy)

    def test_preview_write_failure_cannot_reuse_old_plan(self):
        self.file('a.pdf'); d = self.open(); p = self.preview(d)
        with patch('doc_selecter.gui_remaining.write_plan', side_effect=OSError('artificial write failure')):
            d.preview_button.invoke()
            self.wait_preparation(d)
        self.assertIsNone(d.plan); d.confirm()
        self.assertNotExists(confirmation_path(d.settings, p['plan_id'])); self.assertExists(p['operations'][0]['source'])

    def test_failed_rediscovery_clears_authorization_and_selection(self):
        self.file('a.pdf'); d = self.open(); p = self.preview(d)
        with patch('doc_selecter.gui_remaining.discover_remaining', side_effect=PermissionError('artificial denial')):
            d.refresh_button.invoke()
            self.wait_preparation(d)
        self.assertEqual(d.variables, {}); self.assertIsNone(d.discovery); self.assertIsNone(d.plan)
        d.select_all(True); d.preview(); d.confirm()
        self.assertEqual(str(d.confirm_button['state']), 'disabled')
        self.assertNotExists(confirmation_path(d.settings, p['plan_id']))

    def test_categories_not_rewritten_by_independent_archive(self):
        self.file('a.pdf'); app = self.build()
        for category, var in app._selection_vars.items(): var.set(category == '图片截图')
        categories = app._selected_categories(); d = self.open(); p = self.preview(d)
        d.confirm_button.invoke(); self.assertEqual(app._selected_categories(), categories)
        app.undo_button.invoke(); self.restored(p)

    def test_protected_names_not_shown_or_selected(self):
        self.file('合同.pdf'); self.file('.hidden.pdf'); self.file('okay.pdf')
        self.sandbox.add_dir('private-items'); self.file('project/package.json')
        d = self.open(); self.assertEqual(set(d.variables), {'okay.pdf'})
        text = '\n'.join(str(w['text']) for w in d.inner.winfo_children()) + d.status['text']
        self.assertNotIn('合同', text); self.assertNotIn('private-items', text)
        self.assertIn('疑似私人项目 2', text)
        p = self.preview(d); d.confirm_button.invoke()
        details = self.app.skip_text.get('1.0', 'end')
        self.assertIn('疑似私人项目：2 个', details)
        self.assertIn('受保护成套项目：1 个', details)
        self.assertNotIn('合同', details); self.assertNotIn('private-items', details)
        self.app.undo_button.invoke(); self.restored(p)

    def test_repeated_confirm_cannot_execute_again_or_reconfirm_consumed_plan(self):
        self.file('a.pdf'); d = self.open(); p = self.preview(d); d.confirm_button.invoke()
        saved = Path(confirmation_path(d.settings, p['plan_id'])).read_bytes()
        d.confirm(); self.app.on_organize(); self.app.on_confirm()
        self.assertEqual(Path(confirmation_path(d.settings, p['plan_id'])).read_bytes(), saved)
        self.assertEqual(len(self.app.session.steps), 1)
        self.app.undo_button.invoke(); self.restored(p)

    def test_directory_only_history_reopen_undo_without_session_list(self):
        self.sandbox.add_dir('empty'); d = self.open(); p = self.preview(d); d.confirm_button.invoke()
        self.app.on_close(); app = self.build()
        self.assertEqual(app.plan['plan_id'], p['plan_id']); self.assertEqual(app.session.steps, [])
        self.assertEqual(str(app.undo_button['state']), 'normal')
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        app.undo_button.invoke(); self.restored(p)
        self.assertIn('1 个目录', app.daily_status_label['text'])

    def test_completed_history_reopen_never_reuses_archive_confirmation(self):
        self.sandbox.add_dir('empty'); d = self.open(); p = self.preview(d); d.confirm_button.invoke()
        self.app.undo_button.invoke(); self.app.on_close(); app = self.build()
        self.assertIn('已撤销', app.daily_status_label['text']); self.assertIn('1 个目录', app.daily_status_label['text'])
        self.assertEqual(str(app.undo_button['state']), 'disabled'); self.assertEqual(str(app.organize_button['state']), 'disabled')
        self.assertIsNone(app.confirmed_hash)

    def test_opening_details_settings_archive_dialog_changes_no_records(self):
        self.file('a.pdf'); app = self.build(); p = require_gui_preview(app)
        before = self.sandbox.plan_file(p['plan_id']).read_bytes(); plan = json.dumps(app.plan, sort_keys=True)
        app.on_toggle_details(); app.on_toggle_settings(); d = self.open(); d.close()
        self.assertEqual(json.dumps(app.plan, sort_keys=True), plan)
        self.assertEqual(self.sandbox.plan_file(p['plan_id']).read_bytes(), before)
        self.assertNotExists(confirmation_path(app.settings, p['plan_id'])); self.assertExists(p['operations'][0]['source'])

    def test_minimum_window_scroll_buttons_visible_and_close_lifecycle(self):
        for i in range(45): self.file(f'file{i}.pdf')
        app = self.build(); app.root.geometry('900x720'); d = self.open(); d.window.geometry('760x620')
        app.root.update(); d.window.update()
        for w, window in [(app.remaining_button, app.root), (app.source_button, app.root),
                          (app.primary_button, app.root), (d.all_button, d.window),
                          (d.preview_button, d.window), (d.close_button, d.window)]:
            self.assertTrue(w.winfo_ismapped())
            self.assertGreaterEqual(w.winfo_rooty(), window.winfo_rooty())
            self.assertLessEqual(w.winfo_rooty()+w.winfo_height(), window.winfo_rooty()+window.winfo_height())
            self.assertLessEqual(w.winfo_rootx()+w.winfo_width(), window.winfo_rootx()+window.winfo_width())
        self.assertEqual(len(d.variables), 45)
        app.on_close(); self.assertFalse(d.window.winfo_exists())

    def test_local_file_conflict_warns_not_all_success_and_keeps_undo(self):
        self.file('a.pdf'); self.file('b.pdf'); d = self.open(); p = self.preview(d)
        # Existing parent identities are persisted by a fresh preview, not reset to allow a conflict.
        target = Path(p['operations'][0]['dest']); target.parent.mkdir(parents=True); target.write_bytes(b'EXTERNAL')
        d.refresh_button.invoke(); p = self.preview(d)
        # Fresh batch path differs; construct collision immediately before the real rename.
        original = execute_plan
        def collided(settings, pid, hash_value, **kw):
            def hook(event, op):
                if event == 'before_move' and op['op_id'] == 'op-0001': Path(op['dest']).write_bytes(b'EXTERNAL')
            return original(settings, pid, hash_value, hook=hook, **kw)
        with patch('doc_selecter.gui_remaining.execute_plan', side_effect=collided): d.confirm_button.invoke()
        self.assertIn('未全部完成', d.status['text']); self.assertEqual(self.app.session.active[-1].moved, 1)
        self.assertIn('1 个同名文件已存在，没有覆盖', self.app.problem_label['text'])
        self.app.undo_button.invoke()
        self.assertExists(p['operations'][0]['source']); self.assertExists(p['operations'][1]['source'])

    def test_directory_failure_is_visible_and_tracks_uncertain_batch(self):
        self.sandbox.add_dir('empty'); d = self.open(); p = self.preview(d)
        original = execute_plan
        def changed(settings, pid, hash_value, **kw):
            def hook(event, op):
                if event == 'before_directory_move': Path(op['source'], 'new.pdf').write_bytes(b'EXTERNAL')
            return original(settings, pid, hash_value, hook=hook, **kw)
        with patch('doc_selecter.gui_remaining.execute_plan', side_effect=changed): d.confirm_button.invoke()
        self.assertIn('未全部完成', d.status['text'])
        self.assertIn('检查', self.app.daily_status_label['text'])
        self.assertIn('检查', self.app.problem_label['text'])
        self.assertEqual(self.app.session.active[-1].uncertain_dirs, 1)
        self.assertEqual(str(self.app.undo_button['state']), 'disabled')
        self.assertExists(Path(p['directory_operations'][0]['dest'])/'new.pdf')

    def test_cross_source_directory_batches_return_v0_from_operation_record(self):
        self.sandbox.add_dir('empty'); d = self.open(); p1 = self.preview(d); d.confirm_button.invoke()
        other = self.sandbox.root/'Other'; other.mkdir(); (other/'second').mkdir()
        require_gui_source(self.app, str(other)); d.refresh_button.invoke()
        p2 = self.preview(d); d.confirm_button.invoke()
        self.app.operations_button.invoke(); self.app.return_zero_button.invoke()
        self.assertEqual(self.app.session.head, 0)
        self.assertEqual(self.app._chain_result.completed, [2, 1])
        for p in (p1, p2): self.restored(p)
        self.assertIn('2 个原目录', self.app.daily_status_label['text'])

    def test_busy_confirmation_prevents_nested_archive_and_source_switch(self):
        self.file('a.pdf'); d = self.open(); p = self.preview(d)
        other = self.sandbox.root/'Other'; other.mkdir()
        calls = []
        def nested(*a, **kw):
            calls.append(True); d.confirm(); self.app.on_remaining_archive()
            self.assertFalse(self.app.select_source(str(other)))
            self.assertEqual(str(self.app.remaining_button['state']), 'disabled')
            return True
        self.ask.side_effect = nested; d.confirm_button.invoke()
        self.assertEqual(len(calls), 1); self.assertEqual(len(self.app.session.steps), 1)
        self.ask.side_effect = None; self.app.undo_button.invoke(); self.restored(p)
