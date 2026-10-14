"""来源说明：真实 Tk 控件、可信人工配置和原产品整理/撤销回调。"""
import copy
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from doc_selecter import gui
from doc_selecter.config import ensure_state_dirs
from doc_selecter.plan_hash import compute_plan_hash
from doc_selecter.planner import build_plan, write_plan
from tests.harness import SandboxTestCase, require_gui_source
from tests import test_gui as gui_tests
from tests import test_gui_daily as daily_tests


class SourceGuiTests(SandboxTestCase):
    _stub_dialogs = gui_tests.GuiTests._stub_dialogs
    _restore_dialogs = gui_tests.GuiTests._restore_dialogs
    seed = daily_tests.DailyGuiTests.seed
    state_bytes = daily_tests.DailyGuiTests.state_bytes
    assert_restored = daily_tests.DailyGuiTests.assert_restored
    main_labels = daily_tests.DailyGuiTests.main_labels

    def setUp(self):
        super().setUp()
        self.assertTrue(gui_tests.DISPLAY_AVAILABLE, gui_tests._DISPLAY_ERROR)
        self.app = self.root = None
        self.before = {}

    def tearDown(self):
        if self.app:
            self.app.on_close()
        super().tearDown()

    def build(self, settings=None, *, selected=True):
        settings = settings or self.sandbox.settings
        self._stub_dialogs()
        ensure_state_dirs(settings)
        self.root = gui.tk.Toplevel(gui_tests._SHARED_ROOT)
        self.root.withdraw()
        self.app = gui.App(self.root, settings)
        if selected:
            require_gui_source(self.app, settings.downloads_dir)
        else:
            self.assertFalse(self.app.select_source(settings.downloads_dir))
        return self.app

    def test_before_preview_identifies_configured_source_and_child_selection(self):
        app = self.build()
        self.assertEqual(app.source_label['text'], '整理位置：Downloads（人工测试目录）')
        self.assertEqual(app.pick_button['text'], '选择子文件夹…')
        self.assertIn('上述文件夹的顶层文件', app.source_hint_label['text'])
        self.assertEqual(app.scope_label['text'], '扫描范围：仅顶层')
        self.assertIn('上述文件夹内的「下载分类」', app.target_label['text'])
        self.assertNotIn('当前目录', app.target_label['text'])
        self.assertIsNone(app.plan)
        self.assertIsNone(app.confirmed_hash)
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        self.assertEqual(self.state_bytes(), {})
        self.assertNotIn(str(self.sandbox.downloads), self.main_labels())

    def test_source_uses_actual_configured_name_and_mode_independent_of_cwd(self):
        source = self.sandbox.add_dir('待整理资料')
        other = self.sandbox.root / '另一个工作目录'
        other.mkdir()
        settings = replace(self.sandbox.settings, downloads_dir=str(source), sandboxed=False)
        original_cwd = os.getcwd()
        try:
            os.chdir(other)
            app = self.build(settings)
            self.assertEqual(app.source_label['text'], '整理位置：待整理资料（已选择目录）')
            self.assertNotIn(other.name, self.main_labels())
            self.assertIn(str(source), app.path_label['text'])
            self.assertNotIn(str(source), self.main_labels())
        finally:
            os.chdir(original_cwd)

    def test_sensitive_configured_source_and_target_names_remain_redacted(self):
        source = self.sandbox.add_dir('身份证私密资料')
        settings = replace(self.sandbox.settings, downloads_dir=str(source),
                           target_parent_name='银行卡分类')
        app = self.build(settings, selected=False)
        labels = self.main_labels()
        for name in ('身份证', '私密资料', '银行卡'):
            self.assertNotIn(name, labels)
        self.assertFalse(app._source_selected)
        self.assertIn('尚未选择', app.source_label['text'])
        self.assertIn('敏感信息已隐藏', app.target_label['text'])
        self.assertIsNone(app.plan)
        self.assertFalse(list(source.iterdir()))

    def test_source_persists_through_top_level_roundtrip_and_history_reopen(self):
        self.seed()
        app = self.build()
        source = app.source_label['text']
        app.preview_button.invoke()
        self.assertEqual(app.source_label['text'], source)
        plan = copy.deepcopy(app.plan)
        app.organize_button.invoke()
        self.assertIn('已整理 3', app.daily_status_label['text'])
        self.assertEqual(app.source_label['text'], source)
        destinations = {op['rel_path']: Path(op['dest']) for op in plan['operations']}
        self.assertEqual(
            destinations['说明.md'],
            Path(app.settings.target_root) / '文档资料' / 'Markdown' / '说明.md',
        )
        self.assertEqual(
            destinations['图片.png'],
            Path(app.settings.target_root) / '图片截图' / '图片.png',
        )
        self.assertEqual(
            destinations['数据.csv'],
            Path(app.settings.target_root) / '代码与数据' / '数据.csv',
        )
        app.undo_button.invoke()
        self.assert_restored(plan)
        self.assertEqual(app.source_label['text'], source)
        before = self.state_bytes()
        app.on_close()
        app = self.build()
        self.assertEqual(app.source_label['text'], source)
        self.assertIn('已还原 3', app.daily_status_label['text'])
        self.assertIsNone(app.confirmed_hash)
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        self.assertEqual(self.state_bytes(), before)

    def test_deep_scope_and_preserved_destination_chain_match_source(self):
        self.seed(('Chosen/deep/说明.pdf', 'Chosen/deep/Sub/说明.pdf', '普通.csv'))
        self.sandbox.add_file('Sibling/未选.pdf', b'ARTIFICIAL UNSELECTED')
        app = self.build()
        source = app.source_label['text']
        app.selected_scope = ['Chosen/deep']
        app.subfolder_var.set(True)
        app.on_toggle_subfolders()
        self.assertIn('顶层 + Chosen/deep（递归）', app.scope_label['text'])
        app.preview_button.invoke()
        rows = {app.tree.item(i, 'values')[0]: app.tree.item(i, 'values')[5]
                for i in app.tree.get_children()}
        self.assertEqual(rows['Chosen/deep/说明.pdf'], '下载分类/文档资料/Chosen/deep')
        self.assertEqual(rows['Chosen/deep/Sub/说明.pdf'], '下载分类/文档资料/Chosen/deep/Sub')
        self.assertNotIn('Sibling/未选.pdf', rows)
        plan = copy.deepcopy(app.plan)
        app.organize_button.invoke()
        self.assertEqual(app.source_label['text'], source)
        app.undo_button.invoke()
        self.assert_restored(plan)
        before = self.state_bytes()
        app.on_close()
        app = self.build()
        self.assertEqual(app.selected_scope, [])
        self.assertEqual(app.plan['selected_scope'], ['', 'Chosen/deep'])
        self.assertEqual(app.source_label['text'], source)
        self.assertEqual(app.scope_label['text'], '扫描范围：仅顶层')
        self.assertFalse(app.subfolder_var.get())
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual((self.sandbox.downloads/'Sibling/未选.pdf').read_bytes(), b'ARTIFICIAL UNSELECTED')

    def test_historical_self_reported_root_cannot_replace_current_source(self):
        self.seed(('普通.csv',))
        settings = self.sandbox.settings
        history = build_plan(settings).plan
        history['downloads_dir'] = str(self.sandbox.root / '身份证历史根')
        history['plan_hash'] = compute_plan_hash(history, resolve_paths=False)
        write_plan(settings, history)
        before = self.state_bytes()
        app = self.build()
        self.assertIsNone(app.plan, '不将其他自报根的历史载入当前来源')
        self.assertEqual(app.source_label['text'], '整理位置：Downloads（人工测试目录）')
        self.assertNotIn('身份证历史根', self.main_labels())
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        app.on_organize()
        self.assertEqual(self.state_bytes(), before)
        self.assertIsNone(app.confirmed_hash)
        self.assertEqual((self.sandbox.downloads/'普通.csv').read_bytes(), self.before['普通.csv'][1])

    def test_details_source_is_read_only_for_saved_confirmed_plan_and_logs(self):
        self.seed()
        app = self.build()
        app.on_preview()
        app.on_confirm()
        before = self.state_bytes()
        plan = copy.deepcopy(app.plan)
        confirmed = app.confirmed_hash
        settings = app.settings
        for _ in range(2):
            app.details_button.invoke()
            self.root.update()
            self.assertIn(str(settings.downloads_dir), app.path_label['text'])
            app.settings_button.invoke()
            self.root.update()
        self.assertIs(app.settings, settings)
        self.assertEqual(app.plan, plan)
        self.assertEqual(app.confirmed_hash, confirmed)
        self.assertEqual(self.state_bytes(), before)
        for name, (identity, data) in self.before.items():
            from doc_selecter.atomic_move import lstat_identity
            self.assertEqual(lstat_identity(str(self.sandbox.downloads/name)).as_dict(), identity)
            self.assertEqual((self.sandbox.downloads/name).read_bytes(), data)

    def test_failed_preview_keeps_source_and_rejects_stale_plan(self):
        self.seed()
        app = self.build()
        app.on_preview()
        source = app.source_label['text']
        before = self.state_bytes()
        with patch.object(gui, 'write_plan', side_effect=OSError('ARTIFICIAL save failure')):
            app.preview_button.invoke()
        self.assertEqual(app.source_label['text'], source)
        self.assertEqual(str(app.organize_button['state']), 'disabled')
        app.on_organize()
        self.assertEqual(self.state_bytes(), before)
        self.assertIsNone(app.confirmed_hash)

    def test_minimum_window_keeps_long_source_preview_and_operations_in_bounds(self):
        source = self.sandbox.add_dir('人工文件目录' * 12)
        app = self.build(replace(self.sandbox.settings, downloads_dir=str(source)))
        self.root.geometry('900x720+3000+3000')
        self.root.deiconify()
        self.root.update()
        for name in ('source_label', 'scope_label', 'source_hint_label', 'target_label',
                     'tree', 'preview_button', 'organize_button', 'undo_button', 'daily_status_label'):
            widget = getattr(app, name)
            self.assertTrue(widget.winfo_viewable(), name)
            self.assertGreater(widget.winfo_height(), 18, name)
            current = widget
            while current is not self.root:
                parent = current.master
                x = current.winfo_rootx() - parent.winfo_rootx()
                y = current.winfo_rooty() - parent.winfo_rooty()
                self.assertGreaterEqual(x, 0, name)
                self.assertGreaterEqual(y, 0, name)
                self.assertLessEqual(x + current.winfo_width(), parent.winfo_width(), name)
                self.assertLessEqual(y + current.winfo_height(), parent.winfo_height(), name)
                current = parent
        self.assertGreaterEqual(app.tree.winfo_height(), 50)
