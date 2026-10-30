"""预览及往返后，关键操作和发现控件必须留在窗口可见区域。"""
from unittest.mock import patch
import tkinter as tk

from doc_selecter import gui
from doc_selecter.atomic_move import lstat_identity
from doc_selecter.watcher import NewFileWatcher
from tests.harness import SandboxTestCase, require_gui_source, require_gui_preview
from tests.test_gui import _SHARED_ROOT


class GuiLayoutTests(SandboxTestCase):
    def test_new_filename_visible_while_detail_changes_only_on_explicit_preview(self):
        self.sandbox.add_file('existing.pdf', b'ARTIFICIAL')
        root = tk.Toplevel(_SHARED_ROOT)
        app = gui.App(root, self.sandbox.settings)
        require_gui_source(app, self.sandbox.settings.downloads_dir)
        self.addCleanup(app.on_close)
        require_gui_preview(app)
        plan = app.plan
        old_bytes = self.sandbox.plan_file(plan['plan_id']).read_bytes()
        self.sandbox.add_file('新增测试.txt', b'ARTIFICIAL NEW')
        path = str(self.sandbox.downloads / '新增测试.txt')
        identity = lstat_identity(path)
        clock = [0]
        watcher = NewFileWatcher(clock=lambda: clock[0])
        watcher.enable()
        options = dict(downloads_dir=str(self.sandbox.downloads), target_parent_name='下载分类',
                       extra_ignored=app._known_paths())
        watcher.check_once(**options)
        clock[0] = 10
        report = watcher.check_once(**options)
        self.assertEqual(report.new_files, ['新增测试.txt'])
        app._set_watch_text(watcher.format_report(report))
        root.deiconify()
        for size in ('900x720', '1100x760'):
            root.geometry(size + '+3000+3000')
            root.update_idletasks()
            root.update()
            index = app.watch_text.search('新增测试.txt', '1.0', stopindex='end')
            self.assertTrue(index)
            line = app.watch_text.dlineinfo(index)
            self.assertIsNotNone(line, '首屏必须看见新增文件名')
            self.assertLessEqual(line[1] + line[3], app.watch_text.winfo_height() - 2)
            self.assertTrue(app.watch_scroll.winfo_viewable())
        self.assertIs(app.plan, plan, '发现提示不得自动改计划')
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(), old_bytes)
        self.assertNotIn('新增测试.txt', [app.tree.item(i, 'values')[0] for i in app.tree.get_children()])
        self.assertEqual(lstat_identity(path), identity)
        require_gui_preview(app)
        self.assertNotEqual(app.plan['plan_id'], plan['plan_id'])
        self.assertIn('新增测试.txt', [app.tree.item(i, 'values')[0] for i in app.tree.get_children()])
        self.assertEqual(lstat_identity(path), identity)
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(), old_bytes)

    def test_discovery_and_file_review_remain_visible_after_preview_and_roundtrip(self):
        self.sandbox.add_file('top.pdf', b'ARTIFICIAL')
        self.sandbox.add_file('Project/note.txt', b'ARTIFICIAL')
        root = tk.Toplevel(_SHARED_ROOT)
        app = gui.App(root, self.sandbox.settings)
        require_gui_source(app, self.sandbox.settings.downloads_dir)
        self.addCleanup(app.on_close)
        for name in ('showinfo', 'showwarning', 'showerror'):
            self.enterContext(patch.object(gui.messagebox, name))
        self.enterContext(patch.object(gui.messagebox, 'askokcancel', return_value=True))
        root.deiconify()

        def visible(stage):
            for size in ('900x720', '1100x760', '1280x900', '1100x760'):
                with self.subTest(stage=stage, size=size):
                    root.geometry(size + '+3000+3000')
                    root.update_idletasks()
                    root.update()
                    content = 'empty_frame' if stage in ('initial', 'undone') else 'tree'
                    groups = [(root, (content, 'primary_button', 'undo_button',
                                      'scope_label', 'footer_hint_label'))]
                    if app.watch_results.winfo_viewable():
                        groups.append((root, ('watch_text',)))
                    # 原详情/设置控件仍接受相同的逐层裁切断言，只移入各自入口。
                    app.details_window.deiconify()
                    app.settings_window.deiconify()
                    for window in (app.details_window, app.settings_window):
                        window.geometry(size + '+3000+3000')
                    app.detail_notebook.select(1)
                    root.update()
                    groups.extend([(app.details_window, ('skip_text',)),
                                   (app.settings_window, ('watch_check', 'watch_now_button'))])
                    def in_bounds(owner, names):
                        for name in names:
                            widget = getattr(app, name)
                            self.assertTrue(widget.winfo_viewable(), name)
                            self.assertGreater(widget.winfo_height(), 18, name)
                            current = widget
                            while current is not owner:
                                parent = current.master
                                x = current.winfo_rootx() - parent.winfo_rootx()
                                y = current.winfo_rooty() - parent.winfo_rooty()
                                self.assertGreaterEqual(x, 0, name)
                                self.assertGreaterEqual(y, 0, name)
                                self.assertLessEqual(x + current.winfo_width(), parent.winfo_width(), name)
                                self.assertLessEqual(y + current.winfo_height(), parent.winfo_height(), name)
                                current = parent
                    for owner, names in groups:
                        in_bounds(owner, names)
                    app.detail_notebook.select(2)
                    root.update()
                    in_bounds(app.details_window, ('confirm_button', 'execute_button'))
                    app.details_window.withdraw()
                    app.settings_window.withdraw()
                    self.assertGreaterEqual(getattr(app, content).winfo_height(), 50,
                                            '保留可核对文件明细的空间，不能只显示操作按钮')

        visible('initial')
        require_gui_preview(app)
        visible('top_level_preview')
        app.selected_scope = ['Project']
        app.subfolder_var.set(True)
        app.on_toggle_subfolders()
        visible('recursive_preview')
        app.on_confirm()
        app.on_execute()
        self.assertFalse((self.sandbox.downloads / 'Project/note.txt').exists())
        visible('executed')
        app.on_undo()
        self.assertTrue((self.sandbox.downloads / 'Project/note.txt').is_file())
        visible('undone')
