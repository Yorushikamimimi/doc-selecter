"""Direction B presentation, using artificial files and existing safety callbacks."""
from pathlib import Path
from unittest.mock import patch
import tkinter as tk

from doc_selecter import gui
from doc_selecter.atomic_move import lstat_identity
from tests.harness import SandboxTestCase, require_gui_source, require_gui_preview
from tests.test_gui import _SHARED_ROOT


class DirectionBTests(SandboxTestCase):
    def build(self, selected=True):
        for name in ('showinfo', 'showwarning', 'showerror'):
            self.enterContext(patch.object(gui.messagebox, name))
        self.enterContext(patch.object(gui.messagebox, 'askokcancel', return_value=True))
        root = tk.Toplevel(_SHARED_ROOT)
        app = gui.App(root, self.sandbox.settings)
        self.addCleanup(app.on_close)
        if selected:
            require_gui_source(app, str(self.sandbox.downloads))
        root.geometry('1100x760+3000+3000'); root.update()
        return app

    def test_neutral_source_skips_preview_history_without_stale_warning(self):
        self.sandbox.add_file('人工.pdf')
        plan = self.sandbox.make_plan(confirm=False)
        before = self.sandbox.plan_file(plan['plan_id']).read_bytes()
        app = self.build()
        self.assertIsNone(app.plan)
        self.assertEqual(app.mode_label['text'], '未预览')
        self.assertEqual(app.problem_label['text'], '')
        self.assertTrue(app.empty_frame.winfo_viewable())
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(), before)
        self.assertTrue(Path(plan['operations'][0]['source']).is_file())

    def test_restore_skips_newer_preview_and_keeps_executed_undo(self):
        self.sandbox.add_file('人工.pdf')
        app = self.build(); require_gui_preview(app); app.on_organize()
        old = app.plan
        self.sandbox.add_file('后来.pdf'); app.on_preview()
        self.assertNotEqual(app.plan['plan_id'], old['plan_id'])
        app = self.build()
        self.assertEqual(app.plan['plan_id'], old['plan_id'])
        self.assertEqual(str(app.undo_button['state']), 'normal')
        self.assertIsNone(app.confirmed_hash)
        self.assertNotIn('当前方案已失效', app.daily_status_label['text'])

    def test_filter_and_category_change_keep_files_untouched(self):
        for name in ('人工.pdf', '图片.png', '代码.py'):
            self.sandbox.add_file(name)
        app = self.build(); require_gui_preview(app)
        plan = app.plan; saved = self.sandbox.plan_file(plan['plan_id']).read_bytes()
        identities = {op['source']: lstat_identity(op['source']) for op in plan['operations']}
        app.on_filter_category('文档资料')
        self.assertEqual(tuple(app.tree['displaycolumns']), ('rel', 'format', 'size', 'folder'))
        row = app.tree.item(app.tree.get_children()[0], 'values')
        self.assertEqual(row[0], '人工.pdf')
        self.assertEqual(row[5], '下载分类/文档资料/PDF')
        self.assertEqual(row[6], 'PDF')
        self.assertIs(app.plan, plan)
        app._selection_vars['图片截图'].set(False); app.on_toggle_category()
        self.assertNotEqual(app.plan['plan_id'], plan['plan_id'])
        self.assertNotIn('图片截图', app.plan['category_selection'])
        self.assertIsNone(app.confirmed_hash)
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(), saved)
        self.assertEqual({p: lstat_identity(p) for p in identities}, identities)

    def test_single_primary_empty_preview_completed_and_undone(self):
        self.sandbox.add_file('人工.pdf')
        app = self.build(selected=False)
        def visible_primary(expected):
            app.root.update()
            visible = [w for w in (app.empty_action, app.preview_button, app.organize_button) if w.winfo_viewable()]
            self.assertEqual(visible, [app.primary_button])
            self.assertEqual(app.primary_button['text'], expected)
            self.assertEqual(str(app.primary_button['default']), 'active')
        visible_primary('选择文件夹…')
        require_gui_source(app, str(self.sandbox.downloads)); visible_primary('生成预览')
        require_gui_preview(app); visible_primary('确认整理 1 个文件')
        app.on_organize(); visible_primary('重新生成预览')
        self.assertEqual(app.banner['background'], '#e5f4ec')
        self.assertEqual(app.tree.item(app.tree.get_children()[0], 'values')[7], '已移动')
        self.assertTrue(app.finder_button.winfo_viewable())
        with patch.object(gui.subprocess, 'Popen') as opened:
            app.on_open_target()
            opened.assert_called_once_with(['/usr/bin/open', app.settings.target_root])
        app.on_undo(); visible_primary('生成预览')
        self.assertEqual(app.banner['background'], '#f1f1f4')
        self.assertFalse(app.finder_button.winfo_viewable())
        self.assertTrue((self.sandbox.downloads / '人工.pdf').is_file())

    def test_finder_rejects_missing_or_symlink_target(self):
        app = self.build(); app._history_view = {'phase': 'executed'}
        self.assertFalse(app._target_can_open())
        target = Path(app.settings.target_root)
        target.symlink_to(self.sandbox.downloads, target_is_directory=True)
        with patch.object(gui.subprocess, 'Popen') as opened:
            self.assertFalse(app._target_can_open()); app.on_open_target(); opened.assert_not_called()

    def test_discovery_uses_neutral_banner_without_changing_preview(self):
        self.sandbox.add_file('人工.pdf')
        app = self.build(); require_gui_preview(app); plan = app.plan
        app._set_watch_text('自动检查结果（仅提示）\n    + 新文件.pdf')
        app.root.update()
        self.assertEqual(app.banner['background'], '#f1f1f4')
        self.assertIn('发现新文件', app.banner_title['text'])
        self.assertIs(app.plan, plan)
        self.assertTrue(Path(plan['operations'][0]['source']).is_file())

    def test_completed_collision_is_yellow_row_with_reason_and_keeps_both_files(self):
        self.sandbox.add_file('人工.pdf', b'ARTIFICIAL SOURCE')
        self.sandbox.add_file('图片.png')
        blocker = self.sandbox.target_dir('文档资料') / 'PDF' / '人工.pdf'
        blocker.parent.mkdir(parents=True); blocker.write_bytes(b'ARTIFICIAL CONFLICT')
        app = self.build(); require_gui_preview(app); app.on_organize()
        rows = [(app.tree.item(i, 'values'), app.tree.item(i, 'tags')) for i in app.tree.get_children()]
        skipped = [(v, t) for v, t in rows if v[0] == '人工.pdf'][0]
        self.assertIn('skipped', skipped[1])
        self.assertTrue(skipped[0][7].startswith('跳过 · '))
        self.assertIn('result', app.tree['displaycolumns'])
        self.assertEqual(blocker.read_bytes(), b'ARTIFICIAL CONFLICT')
        self.assertEqual((self.sandbox.downloads / '人工.pdf').read_bytes(), b'ARTIFICIAL SOURCE')
        self.assertEqual(app._category_widgets['文档资料'][4]['text'], '0 +1')
        app.on_undo()
        self.assertTrue((self.sandbox.downloads / '图片.png').is_file())
        self.assertEqual(blocker.read_bytes(), b'ARTIFICIAL CONFLICT')
