"""日常信息层级与合并整理入口；真实 Tk 控件、原产品回调及人工文件。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

from tests.harness import SandboxTestCase, require_gui_preview
from tests import test_gui as gui_tests
from doc_selecter import gui
from doc_selecter.atomic_move import lstat_identity
from doc_selecter.locking import current_phase
from doc_selecter.planner import confirmation_path
from doc_selecter.snapshot import read_snapshot
from doc_selecter.undo import UndoRefused
from doc_selecter.watcher import NewFileWatcher


class DailyGuiTests(SandboxTestCase):
    _build = gui_tests.GuiTests._build
    _stub_dialogs = gui_tests.GuiTests._stub_dialogs
    _restore_dialogs = gui_tests.GuiTests._restore_dialogs

    def setUp(self):
        super().setUp()
        self.assertTrue(gui_tests.DISPLAY_AVAILABLE, gui_tests._DISPLAY_ERROR)
        self.root = self.app = None
        self.before = {}

    def tearDown(self):
        if self.app:
            self.app.on_close()
        elif self.root:
            self.root.destroy()
        super().tearDown()

    def build(self):
        self.app = self._build()
        return self.app

    def seed(self, names=('说明.md', '图片.png', '数据.csv')):
        for name in names:
            p = self.sandbox.add_file(name, b'ARTIFICIAL-' + name.encode())
            self.before[name] = (lstat_identity(str(p)).as_dict(), p.read_bytes())

    def preview(self, scope=None):
        app = self.build()
        if scope:
            app.subfolder_var.set(True)
            app.selected_scope = scope
            app.on_toggle_subfolders()
        require_gui_preview(app)
        return app

    def state_bytes(self):
        return {str(p.relative_to(self.sandbox.state)): p.read_bytes()
                for p in self.sandbox.state.rglob('*') if p.is_file()}

    def assert_restored(self, plan):
        for op in plan['operations']:
            identity, data = self.before[op['rel_path']]
            self.assertEqual(lstat_identity(op['source']).as_dict(), identity)
            self.assertEqual(Path(op['source']).read_bytes(), data)
            self.assertFalse(Path(op['dest']).exists())
        self.assertEqual(current_phase(self.sandbox.settings, plan['plan_id']), 'undone')
        snap = read_snapshot(self.sandbox.settings, plan['plan_id'])
        self.assertEqual(snap['undo_status'], 'completed')
        self.assertTrue(all(e['undo_state'] == 'restored' for e in snap['entries']))
        for kind in ('execution', 'undo'):
            self.assertEqual(self.sandbox.journal(plan['plan_id'], kind).classify().classification, 'healthy')

    def main_labels(self):
        def walk(w):
            if w.winfo_toplevel() is not self.root:
                return
            if isinstance(w, (gui.ttk.Label, gui.tk.Label)):
                yield str(w['text'])
            for c in w.winfo_children():
                yield from walk(c)
        return '\n'.join(walk(self.root))

    def test_default_daily_view_hides_technical_fields_but_details_keep_them(self):
        self.seed()
        app = self.preview()
        self.root.deiconify()
        self.root.update()
        labels = self.main_labels()
        for private in ('sha256:', '计划编号', str(self.sandbox.downloads), '逻辑大小', '未处理 0'):
            self.assertNotIn(private, labels)
        self.assertEqual(app.details_window.state(), 'withdrawn')
        self.assertEqual(app.settings_window.state(), 'withdrawn')
        self.assertFalse(app.hash_label.winfo_viewable())
        self.assertFalse(app.watch_text.winfo_viewable())
        self.assertEqual(tuple(app.tree['displaycolumns']), ('rel', 'size', 'folder'))
        self.assertIn('待整理 3', app.daily_status_label['text'])
        self.assertIn('下载分类', app.target_label['text'])
        self.assertFalse(app.problem_label['text'])
        app.on_toggle_details()
        self.root.update()
        self.assertTrue(app.hash_label.winfo_viewable())
        self.assertIn(app.plan['plan_hash'], app.hash_label['text'])
        self.assertIn(app.plan['plan_id'], app.plan_label['text'])
        rows = [app.detail_tree.item(i, 'values') for i in app.detail_tree.get_children()]
        self.assertEqual(len(rows), 3)
        for row, op in zip(rows, app.plan['operations']):
            self.assertEqual(row[1:4], (op['source'], op['dest'], op['classification_basis']))
            self.assertTrue(row[4])

    def test_empty_preview_disables_daily_organize_and_direct_callback_writes_nothing(self):
        app = self.preview()
        before = self.state_bytes()
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        self.assertIn('没有待整理', app.daily_status_label['text'])
        app.on_organize()
        self.assertEqual(self.state_bytes(), before)
        self.assertFalse(Path(confirmation_path(self.sandbox.settings, app.plan['plan_id'])).exists())

    def test_combined_button_roundtrip_keeps_original_five_identities_and_bytes(self):
        self.seed(('说明.md', '图片.png', 'report' + chr(92) + '2026.pdf'))
        app = self.preview()
        original = copy.deepcopy(app.plan)
        plan_bytes = self.sandbox.plan_file(original['plan_id']).read_bytes()
        app.organize_button.invoke()
        self.assertTrue(Path(confirmation_path(self.sandbox.settings, original['plan_id'])).is_file())
        self.assertIn('已整理 3', app.daily_status_label['text'])
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        for op in original['operations']:
            self.assertEqual(lstat_identity(op['dest']).as_dict(), op['identity'])
            self.assertFalse(Path(op['source']).exists())
        app.undo_button.invoke()
        self.assertIn('已还原 3', app.daily_status_label['text'])
        self.assert_restored(original)
        self.assertEqual(self.sandbox.plan_file(original['plan_id']).read_bytes(), plan_bytes)
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        self.assertEqual(str(app.undo_button['state']), 'disabled')

    def test_deep_same_names_show_preserved_target_levels_and_roundtrip(self):
        self.seed(('Chosen/deep/说明.pdf', 'Chosen/other/说明.pdf', '普通.csv'))
        neighbor = self.sandbox.add_file('Other/说明.pdf', b'UNSELECTED')
        neighbor_before = (lstat_identity(str(neighbor)), neighbor.read_bytes())
        app = self.preview(['Chosen/deep', 'Chosen/other'])
        rows = [app.tree.item(i, 'values') for i in app.tree.get_children()]
        self.assertIn('Chosen/deep/说明.pdf', [r[0] for r in rows])
        self.assertIn('Chosen/other/说明.pdf', [r[0] for r in rows])
        self.assertIn('下载分类/文档资料/Chosen/deep', [r[5] for r in rows])
        self.assertIn('下载分类/文档资料/Chosen/other', [r[5] for r in rows])
        app.on_organize()
        asks = [m for k, m in self.dialog_calls if k == 'ask']
        self.assertTrue(any('Chosen/deep' in m and 'Chosen/other' in m and '整理 3 个文件' in m for m in asks))
        app.on_undo()
        self.assert_restored(app.plan)
        self.assertEqual((lstat_identity(str(neighbor)), neighbor.read_bytes()), neighbor_before)

    def test_final_cancel_writes_no_confirmation_and_moves_nothing(self):
        self.seed()
        app = self.preview()
        before = self.state_bytes()
        with patch.object(gui.messagebox, 'askokcancel', return_value=False), \
                patch.object(gui, 'write_confirmation', wraps=gui.write_confirmation) as confirm, \
                patch.object(gui, 'execute_plan', wraps=gui.execute_plan) as execute:
            app.organize_button.invoke()
            self.assertFalse(confirm.called)
            self.assertFalse(execute.called)
        self.assertEqual(self.state_bytes(), before)
        self.assertIsNone(app.confirmed_hash)
        self.assertEqual(str(app.organize_button['state']), 'normal')
        for name, (identity, data) in self.before.items():
            p = self.sandbox.downloads / name
            self.assertEqual(lstat_identity(str(p)).as_dict(), identity)
            self.assertEqual(p.read_bytes(), data)

    def test_undo_cancel_preserves_state_and_target_identities(self):
        self.seed()
        app = self.preview()
        app.on_organize()
        before = self.state_bytes()
        with patch.object(gui.messagebox, 'askokcancel', return_value=False):
            app.on_undo()
        self.assertEqual(self.state_bytes(), before)
        self.assertIn('已整理 3', app.daily_status_label['text'])
        for op in app.plan['operations']:
            self.assertEqual(lstat_identity(op['dest']).as_dict(), op['identity'])
        app.on_undo()
        self.assert_restored(app.plan)

    def test_scope_change_during_final_dialog_cannot_confirm_or_move_old_plan(self):
        self.seed(('Chosen/a.pdf', 'Other/b.pdf'))
        app = self.preview(['Chosen'])
        old_id = app.plan['plan_id']
        def change(*args, **kwargs):
            app.selected_scope = ['Other']
            app._on_scope_changed()
            return True
        with patch.object(gui.messagebox, 'askokcancel', side_effect=change), \
                patch.object(gui, 'write_confirmation', wraps=gui.write_confirmation) as confirm:
            app.on_organize()
            self.assertFalse(confirm.called)
        self.assertNotEqual(app.plan['plan_id'], old_id)
        self.assertFalse(Path(confirmation_path(self.sandbox.settings, old_id)).exists())
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        self.assertIn('失效', app.daily_status_label['text'])
        for name in self.before:
            self.assertTrue((self.sandbox.downloads / name).is_file())

    def test_category_change_and_all_unselected_require_new_explicit_confirmation(self):
        self.seed()
        app = self.preview()
        old = app.plan['plan_id']
        app.on_toggle_settings()
        app._selection_vars['文档资料'].set(False)
        app.on_toggle_category()
        self.assertNotEqual(app.plan['plan_id'], old)
        self.assertIsNone(app.confirmed_hash)
        self.assertFalse(Path(confirmation_path(self.sandbox.settings, app.plan['plan_id'])).exists())
        self.assertEqual(len(app.plan['operations']), 2)
        for v in app._selection_vars.values():
            v.set(False)
        app.on_toggle_category()
        self.assertEqual(app.plan['operations'], [])
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        app.on_organize()
        self.assertTrue((self.sandbox.downloads/'说明.md').is_file())

    def test_each_preview_failure_invalidates_old_plan_in_daily_entry(self):
        for location, error in (('build_plan', OSError('ARTIFICIAL BUILD')),
                                ('write_plan', OSError('ARTIFICIAL WRITE')),
                                ('assert_downloads_exists', gui.ConfigError('ARTIFICIAL CONFIG'))):
            with self.subTest(location=location):
                if not self.before:
                    self.seed()
                    app = self.preview()
                else:
                    app.on_preview()
                old = app.plan['plan_id']
                with patch.object(gui, location, side_effect=error):
                    app.on_preview()
                before = self.state_bytes()
                app.on_organize()
                self.assertEqual(self.state_bytes(), before)
                self.assertEqual(str(app.organize_button['state']), 'disabled')
                self.assertFalse(Path(confirmation_path(self.sandbox.settings, old)).exists())
                self.assertTrue(app.problem_label['text'])
                self.assertEqual(len([p for p in self.sandbox.downloads.iterdir() if p.is_file()]), 3)

    def test_read_failure_and_partial_scan_are_visible_with_details_closed(self):
        self.seed()
        app = self.build()
        real = gui.build_plan
        def with_issue(*args, **kwargs):
            build = real(*args, **kwargs)
            build.plan['excluded']['递归读取失败/扫描变化'] = {'count': 1, 'items': [{'path':'Chosen', 'code':'ARTIFICIAL'}]}
            build.plan['excluded']['元数据读取失败'] = {'count': 1, 'types': {'ARTIFICIAL':1}}
            return build
        with patch.object(gui, 'build_plan', side_effect=with_issue):
            app.on_preview()
        self.assertEqual(app.details_window.state(), 'withdrawn')
        self.assertIn('读取失败', app.problem_label['text'])
        self.assertIn('可能不完整', app.problem_label['text'])
        self.assertIn('重新生成预览', app.problem_label['text'])

    def test_target_conflict_warns_main_and_does_not_claim_all_moved(self):
        self.seed()
        blocker = self.sandbox.target_dir('文档资料')/'Markdown'/'说明.md'
        blocker.parent.mkdir(parents=True)
        blocker.write_bytes(b'ARTIFICIAL-CONFLICT')
        # 冲突材料必须在预览前存在，才能被计划绑定并在执行时真实触发重名跳过。
        app = self.preview()
        app.on_organize()
        self.assertIn('已整理 2', app.daily_status_label['text'])
        self.assertIn('1 个同名文件已存在，没有覆盖', app.problem_label['text'])
        self.assertEqual(blocker.read_bytes(), b'ARTIFICIAL-CONFLICT')
        self.assertTrue((self.sandbox.downloads/'说明.md').exists())
        app.on_undo()
        self.assertIn('已还原 2', app.daily_status_label['text'])
        self.assertNotIn('已还原 3', app.daily_status_label['text'])

    def test_partial_undo_and_failure_are_visible_and_history_reopens_consistently(self):
        self.seed()
        app = self.preview()
        app.on_organize()
        op = app.plan['operations'][0]
        Path(op['source']).write_bytes(b'ARTIFICIAL-OCCUPANT')
        app.on_undo()
        self.assertIn('部分撤销', app.daily_status_label['text'])
        self.assertIn('受阻', app.problem_label['text'])
        self.assertNotIn('已撤销', app.daily_status_label['text'])
        app.on_close()
        reopened = self.build()
        self.assertIn('部分撤销', reopened.daily_status_label['text'])
        self.assertEqual(str(reopened.organize_button['state']), 'disabled')
        before = self.state_bytes()
        reopened.on_organize()
        self.assertEqual(self.state_bytes(), before)

    def test_undo_refusal_keeps_main_problem_and_previous_execution_counts(self):
        self.seed()
        app = self.preview()
        app.on_organize()
        with patch.object(gui, 'undo_plan', side_effect=UndoRefused('ARTIFICIAL-FAILURE')):
            app.on_undo()
        self.assertIn('失败', app.daily_status_label['text'])
        self.assertTrue(app.problem_label['text'])
        self.assertIn('上次执行：成功 3', app.result_label['text'])
        app.on_undo()
        self.assert_restored(app.plan)

    def test_corrupt_history_stays_visible_needs_review_and_bytes_preserved(self):
        self.seed()
        app = self.preview()
        app.on_organize()
        log = Path(self.sandbox.journal(app.plan['plan_id']).generation_file(1))
        log.write_bytes(log.read_bytes() + b'ARTIFICIAL INVALID\n')
        before = self.state_bytes()
        app._refresh_plan_buttons()
        self.assertIn('检查', app.daily_status_label['text'])
        self.assertTrue(app.problem_label['text'])
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        app.on_organize()
        self.assertEqual(self.state_bytes(), before)

    def test_recover_missing_snapshot_then_undo_preserves_original_plan_confirmation(self):
        self.seed()
        app = self.preview()
        app.on_organize()
        plan = self.sandbox.plan_file(app.plan['plan_id'])
        confirmation = Path(confirmation_path(self.sandbox.settings, app.plan['plan_id']))
        original = (plan.read_bytes(), confirmation.read_bytes())
        (self.sandbox.state/'undos'/('undo-'+app.plan['plan_id']+'.json')).unlink()
        app._refresh_plan_buttons()
        self.assertIn('检查', app.daily_status_label['text'])
        self.assertTrue(app.problem_label['text'])
        app.on_recover()
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        app.on_undo()
        self.assert_restored(app.plan)
        self.assertEqual((plan.read_bytes(), confirmation.read_bytes()), original)

    def test_history_reopen_keeps_scope_and_new_preview_is_unconfirmed(self):
        self.seed(('Chosen/deep/说明.pdf', '普通.csv'))
        app = self.preview(['Chosen/deep'])
        app.on_organize()
        app.on_undo()
        old = app.plan['plan_id']
        old_bytes = self.state_bytes()
        app.on_close()
        app = self.build()
        self.assertEqual(app.selected_scope, [])
        self.assertFalse(app.subfolder_var.get())
        self.assertEqual(app.plan['selected_scope'], ['', 'Chosen/deep'])
        self.assertIn('已还原 2', app.daily_status_label['text'])
        self.assertIsNone(app.confirmed_hash)
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        app.on_organize()
        self.assertEqual(self.state_bytes(), old_bytes)
        app.on_preview()
        self.assertNotEqual(app.plan['plan_id'], old)
        self.assertEqual(str(app.organize_button['state']), 'normal')
        self.assertIsNone(app.confirmed_hash)
        self.assertFalse(Path(confirmation_path(self.sandbox.settings, app.plan['plan_id'])).exists())
        for rel, data in old_bytes.items():
            self.assertEqual((self.sandbox.state/rel).read_bytes(), data)
        app.on_organize()
        app.on_undo()
        self.assert_restored(app.plan)

    def test_open_close_details_settings_changes_no_state_identity_or_confirmation(self):
        self.seed()
        app = self.preview()
        before = self.state_bytes()
        original = copy.deepcopy(app.plan)
        for _ in range(2):
            app.on_toggle_details()
            app.on_toggle_settings()
            self.root.update()
        self.assertEqual(app.plan, original)
        self.assertEqual(self.state_bytes(), before)
        self.assertIsNone(app.confirmed_hash)
        for rel, (identity, data) in self.before.items():
            p=self.sandbox.downloads/rel
            self.assertEqual(lstat_identity(str(p)).as_dict(), identity)
            self.assertEqual(p.read_bytes(),data)
        details,settings=app.details_window,app.settings_window
        app.on_close()
        self.app=None
        self.assertFalse(details.winfo_exists())
        self.assertFalse(settings.winfo_exists())

    def test_new_file_names_visible_more_keeps_all_reports_and_old_plan_immutable(self):
        self.seed(('existing.pdf',))
        app = self.preview()
        old = app.plan
        before=self.state_bytes()
        clock=[0]
        watcher=NewFileWatcher(clock=lambda:clock[0]);watcher.enable()
        for i in range(9):self.sandbox.add_file(f'新增{i}.txt',b'ARTIFICIAL')
        options=dict(downloads_dir=str(self.sandbox.downloads),target_parent_name='下载分类',extra_ignored=app._known_paths())
        watcher.check_once(**options);clock[0]=10
        report=watcher.check_once(**options)
        self.assertEqual(len(report.new_files),9)
        app._set_watch_text(watcher.format_report(report))
        self.root.deiconify();self.root.update()
        self.assertTrue(app.watch_text.winfo_viewable())
        self.assertIn('新增0.txt',app.watch_text.get('1.0','end'))
        self.assertTrue(app.watch_more_button.winfo_viewable())
        self.assertIn('重新生成预览',app.watch_text.get('1.0','end'))
        app.watch_more_button.invoke();self.root.update()
        self.assertTrue(app.discovery_detail.winfo_viewable())
        for i in range(9):self.assertIn(f'新增{i}.txt',app.discovery_detail.get('1.0','end'))
        self.assertIs(app.plan,old)
        self.assertEqual(self.state_bytes(),before)
        self.assertIsNone(app.confirmed_hash)
        app.on_preview()
        self.assertEqual(len(app.tree.get_children()),10)

    def test_watch_toggle_is_only_notice_and_does_not_authorize_or_change_plan(self):
        self.seed()
        app=self.preview()
        before=self.state_bytes()
        self.assertFalse(app.watcher.enabled)
        app._watch_enabled_var.set(True);app.on_toggle_watch()
        self.assertTrue(app.watcher.enabled)
        self.assertIsNotNone(app.watch_job)
        self.assertEqual(self.state_bytes(),before)
        self.assertIsNone(app.confirmed_hash)
        app._watch_enabled_var.set(False);app.on_toggle_watch()
        self.assertFalse(app.watcher.enabled)
        self.assertIsNone(app.watch_job)
        self.assertEqual(self.state_bytes(),before)

    def test_execution_refusal_is_visible_without_claiming_success(self):
        self.seed()
        app=self.preview()
        with patch.object(gui, 'execute_plan', side_effect=gui.ExecutionRefused('ARTIFICIAL REFUSAL')):
            app.on_organize()
        self.assertIn('失败',app.daily_status_label['text'])
        self.assertTrue(app.problem_label['text'])
        self.assertNotIn('已整理 3',app.daily_status_label['text'])
        for name in self.before:self.assertTrue((self.sandbox.downloads/name).is_file())

    def test_confirmation_write_failure_invalidates_daily_entry_and_moves_nothing(self):
        self.seed()
        app=self.preview()
        with patch.object(gui, 'write_confirmation', side_effect=OSError('ARTIFICIAL CONFIRM WRITE')), \
                patch.object(gui, 'execute_plan', wraps=gui.execute_plan) as execute:
            app.on_organize()
            self.assertFalse(execute.called)
        self.assertEqual(str(app.organize_button['state']),'disabled')
        self.assertIsNone(app.confirmed_hash)
        for name in self.before:self.assertTrue((self.sandbox.downloads/name).is_file())

    def test_settings_changed_without_callback_during_dialog_is_rejected(self):
        self.seed()
        app=self.preview()
        before=self.state_bytes()
        def change(*args,**kwargs):
            app._selection_vars['文档资料'].set(False)
            return True
        with patch.object(gui.messagebox,'askokcancel',side_effect=change), \
                patch.object(gui,'write_confirmation',wraps=gui.write_confirmation) as confirm:
            app.on_organize()
            self.assertFalse(confirm.called)
        self.assertEqual(self.state_bytes(),before)
        self.assertEqual(str(app.organize_button['state']),'disabled')
        self.assertIsNone(app.confirmed_hash)

    def test_opening_details_settings_does_not_repair_corrupted_history(self):
        self.seed()
        app=self.preview();app.on_organize()
        log=Path(self.sandbox.journal(app.plan['plan_id']).generation_file(1))
        log.write_bytes(log.read_bytes()+b'ARTIFICIAL CORRUPTION\n')
        before=self.state_bytes()
        app.on_toggle_details();app.on_toggle_settings();self.root.update()
        app.on_toggle_details();app.on_toggle_settings();self.root.update()
        self.assertEqual(self.state_bytes(),before)

    def test_partial_discovery_shows_both_problem_and_new_name_on_daily_page(self):
        self.seed()
        app=self.preview()
        app._set_watch_text('自动检查结果（仅提示）\n检查覆盖不完整：1 处读取失败/扫描变化\n    + 新增文件.pdf')
        self.root.deiconify();self.root.update()
        self.assertIn('覆盖不完整',app.problem_label['text'])
        self.assertIn('新增文件.pdf',app.watch_text.get('1.0','end'))
        index=app.watch_text.search('新增文件.pdf','1.0',stopindex='end')
        self.assertIsNotNone(app.watch_text.dlineinfo(index))
        self.assertIn('读取失败',app.discovery_detail.get('1.0','end'))

    def test_history_without_saved_counts_uses_log_counts_in_daily_summary(self):
        self.seed()
        app=self.preview();app.on_organize()
        path=self.sandbox.state/'plan_state'/(app.plan['plan_id']+'.json')
        state=json.loads(path.read_text());state.pop('counts')
        path.write_text(json.dumps(state))
        app._refresh_plan_buttons()
        self.assertIn('已整理 3',app.daily_status_label['text'])
        self.assertIn('成功 3',app.result_label['text'])
        app.on_undo();self.assert_restored(app.plan)

    def test_detail_legacy_dialog_cannot_execute_a_different_new_plan(self):
        self.seed()
        app=self.preview();app.on_confirm()
        old=app.plan['plan_id']
        def change(*args,**kwargs):
            app.on_preview();app.on_confirm()
            return True
        with patch.object(gui.messagebox,'askokcancel',side_effect=change):
            app.on_execute()
        self.assertNotEqual(app.plan['plan_id'],old)
        self.assertEqual(str(app.organize_button['state']),'disabled')
        for name in self.before:self.assertTrue((self.sandbox.downloads/name).is_file())
        self.assertEqual(self.sandbox.journal(old).read_events()[0],[])
        self.assertEqual(self.sandbox.journal(app.plan['plan_id']).read_events()[0],[])
