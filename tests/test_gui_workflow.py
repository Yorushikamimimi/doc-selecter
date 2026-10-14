"""主动来源、可选子目录、未整理原因与本次会话版本链；人工目录和真实 Tk 回调。"""
import copy
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from doc_selecter import gui
from doc_selecter.atomic_move import lstat_identity, AtomicMover, DestinationExists, DirectoryIdentityChanged
from doc_selecter.config import ensure_state_dirs
from doc_selecter.executor import execute_plan
from doc_selecter.gui_session import OperationSession, ReturnReport
from doc_selecter.locking import GlobalLock, read_plan_state
from doc_selecter.planner import confirmation_path
from doc_selecter.snapshot import read_snapshot
from doc_selecter.undo import undo_plan
from tests.harness import SandboxTestCase, require_gui_source, require_gui_preview
from tests import test_gui as gui_tests


class WorkflowBase(SandboxTestCase):
    _stub_dialogs = gui_tests.GuiTests._stub_dialogs
    _restore_dialogs = gui_tests.GuiTests._restore_dialogs

    def setUp(self):
        super().setUp()
        self.assertTrue(gui_tests.DISPLAY_AVAILABLE, gui_tests._DISPLAY_ERROR)
        self.app = self.root = None
        self.originals = {}
        self.plans = []

    def tearDown(self):
        if self.app:
            self.app.on_close()
        super().tearDown()

    def build(self, selected=True):
        self.dialog_calls = self._stub_dialogs()
        ensure_state_dirs(self.sandbox.settings)
        self.root = gui.tk.Toplevel(gui_tests._SHARED_ROOT)
        self.root.withdraw()
        self.app = gui.App(self.root, self.sandbox.settings)
        if selected:
            require_gui_source(self.app, str(self.sandbox.downloads))
        return self.app

    def state_bytes(self):
        return {str(p.relative_to(self.sandbox.state)): p.read_bytes()
                for p in self.sandbox.state.rglob('*') if p.is_file()}

    def seed(self, name, source=None):
        p = (source or self.sandbox.downloads) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        data = b'ARTIFICIAL-' + name.encode()
        p.write_bytes(data)
        self.originals[str(p)] = (lstat_identity(str(p)).as_dict(), data)
        return p

    def batch(self, name, source=None):
        self.seed(name, source)
        app = self.app or self.build()
        require_gui_preview(app)
        plan = copy.deepcopy(app.plan)
        app.organize_button.invoke()
        self.plans.append((app.settings, plan))
        return plan

    def three(self):
        self.build()
        for name in ('first.pdf', 'second.pdf', 'third.pdf'):
            self.batch(name)
        self.assertEqual(self.app.session.head, 3)

    def restored(self):
        for path, (identity, data) in self.originals.items():
            self.assertEqual(lstat_identity(path).as_dict(), identity)
            self.assertEqual(Path(path).read_bytes(), data)
        for settings, plan in self.plans:
            self.assertEqual(read_plan_state(settings, plan['plan_id'])['phase'], 'undone')
            self.assertEqual(read_snapshot(settings, plan['plan_id'])['undo_status'], 'completed')
            for op in plan['operations']:
                self.assertFalse(os.path.lexists(op['dest']))
            from doc_selecter.executor import classify_journals
            self.assertTrue(all(v['state'].healthy for v in classify_journals(settings, plan['plan_id']).values()))


class SourceChoiceTests(WorkflowBase):
    def test_unselected_start_cannot_scan_confirm_organize_or_discover(self):
        self.seed('ordinary.pdf')
        self.sandbox.make_plan(confirm=False)
        app = self.build(selected=False)
        before = self.state_bytes()
        with patch.object(gui, 'build_plan') as scan, patch.object(gui, 'execute_plan') as move:
            app.on_preview(); app.on_confirm(); app.on_organize(); app.perform_execution(); app.on_check_now()
            scan.assert_not_called(); move.assert_not_called()
        self.assertIsNone(app.plan)
        self.assertFalse(app._source_selected)
        self.assertIn('先选择', app.daily_status_label['text'])
        self.assertEqual(str(app.preview_button['state']), 'disabled')
        self.assertEqual(self.state_bytes(), before)


    def test_cancel_system_picker_preserves_source_plan_confirmation_and_bytes(self):
        app = self.build(); self.seed('ordinary.pdf'); app.on_preview(); app.on_confirm()
        before = self.state_bytes(); plan = app.plan; settings = app.settings; confirmed = app.confirmed_hash
        with patch.object(gui.filedialog, 'askdirectory', return_value='') as picker:
            app.source_button.invoke()
        picker.assert_called_once()
        self.assertIs(app.plan, plan); self.assertIs(app.settings, settings)
        self.assertEqual(app.confirmed_hash, confirmed); self.assertEqual(self.state_bytes(), before)

    def test_source_switch_invalidates_confirmation_and_never_loads_other_root(self):
        app = self.build(); self.seed('first.pdf'); app.on_preview(); app.on_confirm()
        old_plan = copy.deepcopy(app.plan); old = self.state_bytes()
        source = self.sandbox.root/'OtherSource'; source.mkdir(); self.seed('other.csv', source)
        with patch.object(gui.filedialog, 'askdirectory', return_value=str(source)):
            app.source_button.invoke()
        self.assertIsNone(app.plan); self.assertIsNone(app.confirmed_hash)
        self.assertEqual(app.selected_scope, []); self.assertFalse(app.subfolder_var.get())
        app.on_organize()
        self.assertEqual(self.state_bytes(), old)
        self.assertTrue(Path(old_plan['operations'][0]['source']).exists())
        app.on_preview()
        self.assertEqual([op['rel_path'] for op in app.plan['operations']], ['other.csv'])
        self.assertEqual(app.plan['downloads_dir'], str(source))

    def test_rejects_state_parent_links_packages_projects_and_sensitive_sources(self):
        app = self.build()
        before = self.state_bytes(); settings = app.settings
        private = self.sandbox.root/'身份证'; private.mkdir()
        package = self.sandbox.root/'Tool.app'; package.mkdir()
        project = self.sandbox.root/'Project'; project.mkdir(); (project/'.git').mkdir()
        project_child = project/'Resources';project_child.mkdir()
        link = self.sandbox.root/'link'; link.symlink_to(self.sandbox.downloads, target_is_directory=True)
        for path in (self.sandbox.root, self.sandbox.state, private, package, project, project_child, link):
            with self.subTest(path=path.name):
                self.assertFalse(app.select_source(str(path)))
                self.assertIs(app.settings, settings)
                self.assertEqual(self.state_bytes(), before)
        messages = '\n'.join(text for _, text in self.dialog_calls)
        self.assertNotIn('身份证', messages)
        # 状态与来源同在项目更深处，也不能以共同父目录当作保护起点漏掉项目根。
        nested = project/'nested';(nested/'Resources').mkdir(parents=True)
        (nested/'state').mkdir()
        base = replace(settings, state_dir=str(nested/'state'), sandboxed=False)
        with self.assertRaises(gui.ConfigError):gui.selected_settings(base,str(nested/'Resources'))

    def test_outside_artificial_root_rejected_before_any_metadata_call(self):
        app = self.build()
        with patch('doc_selecter.gui_source.os.lstat', side_effect=AssertionError('must not stat outside sandbox')):
            self.assertFalse(app.select_source('/never-read-outside-this-sandbox'))

    def test_selected_config_clone_does_not_resolve_paths_again(self):
        app=self.build(); old=app.settings
        source=self.sandbox.root/'Other';source.mkdir()
        with patch('doc_selecter.gui_source.os.path.realpath',side_effect=AssertionError('must not follow path')):
            self.assertTrue(app.select_source(str(source)))
        self.assertEqual(old.downloads_dir,str(self.sandbox.downloads))
        self.assertEqual(app.settings.canonical_downloads_dir,str(source))
        self.assertEqual(app.settings.canonical_state_dir,old.canonical_state_dir)

    def test_switching_back_to_recursive_history_clears_current_scope_without_rewriting_it(self):
        app=self.build();self.seed('Chosen/deep/a.pdf')
        app.selected_scope=['Chosen/deep'];app.subfolder_var.set(True);app.on_toggle_subfolders();app.on_preview()
        plan=copy.deepcopy(app.plan);before=self.state_bytes()
        other=self.sandbox.root/'Other';other.mkdir();require_gui_source(app, str(other))
        require_gui_source(app, str(self.sandbox.downloads))
        self.assertEqual(app.selected_scope,[]);self.assertFalse(app.subfolder_var.get())
        self.assertEqual(app.scope_label['text'],'扫描范围：仅顶层')
        self.assertEqual(app.plan['selected_scope'],['','Chosen/deep'])
        self.assertEqual(app.detail_tree.item(app.detail_tree.get_children()[0],'values')[0],'Chosen/deep/a.pdf')
        self.assertEqual(app.tree.get_children(),())
        self.assertEqual(self.state_bytes(),before);self.assertIsNone(app.confirmed_hash)
        app.on_organize();self.assertEqual(self.state_bytes(),before)

    def test_old_async_scan_discarded_after_source_switch_and_watch_stops(self):
        app = self.build(); self.seed('first.pdf'); app.on_preview()
        source = self.sandbox.root/'OtherSource'; source.mkdir()
        started = threading.Event(); release = threading.Event()
        real = gui.NewFileWatcher.check_once
        def slow(watcher, **opts):
            started.set(); release.wait(3)
            return real(watcher, **opts)
        with patch.object(gui.NewFileWatcher, 'check_once', slow):
            app.on_check_now(); self.assertTrue(started.wait(1))
            require_gui_source(app, str(source)); release.set()
            deadline = time.monotonic()+3
            while app._discovery_thread is not None and time.monotonic()<deadline:
                self.root.update(); time.sleep(.01)
        self.assertIsNone(app._discovery_thread)
        self.assertFalse(app.watcher.enabled); self.assertFalse(app._watch_enabled_var.get())
        self.assertEqual(app.watch_text.get('1.0', 'end').strip(), '')
        self.assertEqual(app.settings.downloads_dir, str(source)); self.assertIsNone(app.plan)

    def test_source_change_in_final_confirmation_cannot_move_original_plan(self):
        app = self.build(); self.seed('first.pdf'); app.on_preview()
        source = self.sandbox.root/'OtherSource'; source.mkdir()
        old = self.state_bytes()
        def change(*a, **k):
            require_gui_source(app, str(source)); return True
        with patch.object(gui.messagebox, 'askokcancel', side_effect=change):
            app.on_organize()
        self.assertEqual(self.state_bytes(), old)
        self.assertTrue((self.sandbox.downloads/'first.pdf').exists())
        self.assertEqual(len(app.session.steps), 0)


class FolderAndRemainingTests(WorkflowBase):
    def folders(self):
        self.seed('A/deep/a.pdf'); self.seed('B/b.pdf'); self.seed('Hidden.app/file.pdf')
        self.seed('身份证/file.pdf'); self.seed('.hidden/file.pdf')
        self.seed('Project/package.json'); self.seed('Project/src.pdf')
        app = self.build(); app.subfolder_var.set(True); app.on_toggle_subfolders(); app.on_pick_folders()
        return app

    def test_select_all_only_eligible_and_normalizes_parent_child_overlap(self):
        app = self.folders()
        self.assertEqual(set(app.folder_vars), {'A', 'B'})
        app.folder_all_button.invoke(); self.assertTrue(all(v.get() for v in app.folder_vars.values()))
        app.folder_confirm_button.invoke()
        self.assertEqual(app.selected_scope, ['A', 'B'])
        app.on_preview()
        self.assertEqual({op['rel_path'] for op in app.plan['operations']}, {'A/deep/a.pdf','B/b.pdf'})
        self.assertIsNone(app.confirmed_hash)
        # 当前产品选择器只列出可选顶层目录；显式父子重叠仍按原准入规范去重。
        app.selected_scope = ['A', 'A/deep', 'B']
        app._on_scope_changed()
        self.assertEqual(app.plan['selected_scope'], ['', 'A', 'A/deep', 'B'])
        self.assertEqual({op['rel_path'] for op in app.plan['operations']}, {'A/deep/a.pdf','B/b.pdf'})
        self.assertEqual(len(app.plan['operations']),2)

    def test_clear_all_cancel_and_confirm_preserve_explicit_authorization(self):
        app = self.folders(); app.folder_all_button.invoke(); app.folder_cancel_button.invoke()
        self.assertEqual(app.selected_scope, [])
        app.on_pick_folders(); app.folder_all_button.invoke(); app.folder_confirm_button.invoke()
        app.on_preview(); app.on_confirm(); original = app.plan; before = self.state_bytes()
        app.on_pick_folders(); app.folder_none_button.invoke(); app.folder_cancel_button.invoke()
        self.assertIs(app.plan, original); self.assertEqual(self.state_bytes(), before)
        app.on_pick_folders(); app.folder_none_button.invoke(); app.folder_confirm_button.invoke()
        self.assertEqual(app.selected_scope, []); self.assertIsNone(app.confirmed_hash)
        self.assertEqual(app.plan['selected_scope'], [''])

    def test_many_folders_scroll_and_action_buttons_remain_visible(self):
        for i in range(70): self.seed(f'Folder{i}/a.pdf')
        app = self.build(); app.subfolder_var.set(True); app.on_toggle_subfolders(); app.on_pick_folders()
        dialog = app.folder_dialog; dialog.geometry('500x360+3000+3000'); dialog.deiconify(); self.root.update()
        self.assertEqual(len(app.folder_vars), 70)
        for button in (app.folder_all_button,app.folder_none_button,app.folder_confirm_button,app.folder_cancel_button):
            self.assertTrue(button.winfo_viewable())
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),dialog.winfo_rooty()+dialog.winfo_height())
        self.assertGreater(app.folder_canvas.bbox('all')[3], app.folder_canvas.winfo_height())
        self.assertNotIn('**', ''.join(str(w.cget('text')) for w in dialog.winfo_children() if isinstance(w, gui.ttk.Label)))

    def test_remaining_counts_privacy_unfinished_and_unselected_are_distinct(self):
        self.seed('说明.md'); self.seed('合同.md'); self.seed('unfinished.part'); self.seed('Unselected/a.pdf')
        app = self.build(); app.on_preview()
        self.assertEqual([op['rel_path'] for op in app.plan['operations']], ['说明.md'])
        text = str(app.remaining_label['text'])
        self.assertIn('疑似私人项目 1', text); self.assertIn('未完成下载 1', text)
        self.assertIn('未选择的文件夹 1', text); self.assertNotIn('失败', text)
        self.assertNotIn('合同', app.skip_text.get('1.0','end'))
        self.assertNotIn('合同', json.dumps(app.plan,ensure_ascii=False))

    def test_protected_project_and_scan_errors_use_actual_reason_counts(self):
        self.seed('Project/package.json'); self.seed('Project/src.pdf'); self.seed('Chosen/a.pdf')
        app = self.build(); app.subfolder_var.set(True); app.selected_scope=['Project','Chosen']; app.on_toggle_subfolders(); app.on_preview()
        self.assertIn('受保护的成套项目 1', app.remaining_label['text'])
        self.assertNotIn('Project/src.pdf', [op['rel_path'] for op in app.plan['operations']])
        view = copy.deepcopy(app.plan)
        view['excluded']['元数据读取失败']={'count':2,'types':{'permission':2}}
        view['excluded']['递归读取失败/扫描变化']={'count':1,'items':[]}
        app._render_plan(view)
        self.assertIn('元数据读取失败 2', app.remaining_label['text'])
        self.assertIn('递归读取失败/扫描变化 1', app.remaining_label['text'])
        self.assertTrue(app.problem_label['text'])


class SessionChainTests(WorkflowBase):
    def test_return_result_counts_all_batches_and_survives_refresh(self):
        self.build()
        for number in (1,2,3):
            for i in range(number):self.seed(f'batch{number}-file{i}.pdf')
            self.app.on_preview();p=copy.deepcopy(self.app.plan);self.app.on_organize()
            self.plans.append((self.app.settings,p))
        self.app.on_return_to(0)
        self.assertIn('已还原 6 个文件',self.app.daily_status_label['text'])
        self.assertIn('撤销结果：已还原 3',self.app.result_label['text'])
        self.app._refresh_plan_buttons()
        self.assertIn('已还原 6 个文件',self.app.daily_status_label['text']);self.restored()

    def test_partial_batch_restores_safe_files_but_stops_before_older_batch(self):
        self.build(); self.batch('older.pdf'); self.seed('a.pdf'); self.seed('b.pdf')
        self.app.on_preview(); plan=copy.deepcopy(self.app.plan)
        self.plans.append((self.app.settings,plan)); self.app.on_organize()
        occupied=self.sandbox.downloads/'b.pdf'; occupied.write_bytes(b'EXTERNAL')
        self.app.on_return_to(0)
        self.assertEqual(self.app.session.head,2)
        self.assertTrue((self.sandbox.downloads/'a.pdf').exists())
        self.assertEqual(lstat_identity(str(self.sandbox.downloads/'a.pdf')).as_dict(),self.originals[str(self.sandbox.downloads/'a.pdf')][0])
        self.assertEqual(occupied.read_bytes(),b'EXTERNAL')
        self.assertFalse((self.sandbox.downloads/'older.pdf').exists())
        self.assertEqual(read_snapshot(self.app.settings,plan['plan_id'])['undo_status'],'partial_undo')
        self.assertIn('确定已还原 1',self.app.problem_label['text'])

    def test_replaced_source_parent_stops_whole_chain(self):
        self.build(); self.batch('older.pdf'); self.seed('Chosen/deep/a.pdf')
        self.app.selected_scope=['Chosen']; self.app.subfolder_var.set(True); self.app.on_toggle_subfolders()
        self.app.on_organize()
        parent=self.sandbox.downloads/'Chosen/deep'
        parent.rename(parent.with_name('original-deep')); parent.mkdir()
        self.app.on_return_to(0)
        self.assertEqual(self.app.session.head,2)
        self.assertFalse((self.sandbox.downloads/'older.pdf').exists())
        self.assertFalse((parent/'a.pdf').exists())
        self.assertIn('未退回',self.app.problem_label['text'])

    def test_source_parent_changed_to_link_does_not_restore_through_link(self):
        self.build(); self.batch('older.pdf'); self.seed('Chosen/deep/a.pdf')
        self.app.selected_scope=['Chosen']; self.app.subfolder_var.set(True); self.app.on_toggle_subfolders()
        self.app.on_organize()
        parent=self.sandbox.downloads/'Chosen/deep'; saved=parent.with_name('original-deep')
        parent.rename(saved); parent.symlink_to(saved,target_is_directory=True)
        self.app.on_return_to(0)
        self.assertEqual(self.app.session.head,2)
        self.assertFalse((saved/'a.pdf').exists())
        self.assertFalse((self.sandbox.downloads/'older.pdf').exists())
        self.assertTrue(parent.is_symlink())

    def test_global_execution_stop_keeps_moved_failed_and_unattempted_distinct(self):
        self.build()
        for name in ('a.pdf','b.pdf','c.pdf'):self.seed(name)
        self.app.on_preview(); real=AtomicMover.rename_excl_at; calls=[0]
        def stopped(mover,*args,**kw):
            calls[0]+=1
            if calls[0]==2:raise DirectoryIdentityChanged('ARTIFICIAL changed directory')
            return real(mover,*args,**kw)
        with patch.object(AtomicMover,'rename_excl_at',stopped):self.app.on_organize()
        step=self.app.session.active[-1]
        self.assertEqual((step.moved,step.failed,step.not_attempted),(1,1,1))
        self.assertIn('失败 1',self.app.problem_label['text']); self.assertIn('未处理 1',self.app.problem_label['text'])
        self.app.on_recover();self.app.on_undo()
        for path,(identity,data) in self.originals.items():
            self.assertEqual(lstat_identity(path).as_dict(),identity)
            self.assertEqual(Path(path).read_bytes(),data)

    def test_unknown_result_after_actual_move_remains_tracked_and_unreturnable(self):
        self.build(); self.seed('a.pdf'); self.app.on_preview(); real=AtomicMover.rename_excl_at
        def unknown(mover,*args,**kw):
            real(mover,*args,**kw)
            raise RuntimeError('ARTIFICIAL unknown result')
        with patch.object(AtomicMover,'rename_excl_at',unknown):self.app.on_organize()
        step=self.app.session.active[-1]
        self.assertEqual(step.uncertain,1)
        self.assertFalse((self.sandbox.downloads/'a.pdf').exists())
        self.app.on_return_to(0)
        self.assertEqual(self.app.session.head,1); self.assertIn('未退回',self.app.problem_label['text'])

    def test_reopen_historical_cancel_is_busy_and_zero_movement(self):
        self.build(); self.batch('a.pdf'); self.app.on_close(); app=self.build(selected=False)
        require_gui_source(app, str(self.sandbox.downloads))
        other=self.sandbox.root/'Other'; other.mkdir(); before=self.state_bytes()
        def cancel(*args,**kw):
            self.assertFalse(app.select_source(str(other)))
            app.on_preview(); app.on_organize(); app.on_undo(); return False
        with patch.object(gui.messagebox,'askokcancel',cancel):app.on_undo()
        self.assertEqual(before,self.state_bytes()); self.assertFalse((self.sandbox.downloads/'a.pdf').exists())
        app.on_undo(); self.restored()

    def test_check_original_batch_after_source_switch_uses_original_settings(self):
        self.build(); self.batch('a.pdf'); first=self.app.settings; pid=self.app.plan['plan_id']
        other=self.sandbox.root/'Other'; other.mkdir(); require_gui_source(self.app, str(other))
        self.app.operations_tree.selection_set('1'); real=gui.recover_execution
        with patch.object(gui,'recover_execution',wraps=real) as check:self.app.on_recover_step()
        self.assertIs(check.call_args.args[0],first); self.assertEqual(check.call_args.args[1],pid)
        self.assertEqual(self.app.settings.downloads_dir,str(other)); self.assertIsNone(self.app.plan)
        self.app.on_undo(); self.restored()

    def test_selected_record_shows_complete_original_source_without_writing_state(self):
        self.build();self.batch('a.pdf');source=self.app.settings.downloads_dir
        other=self.sandbox.root/'Other';other.mkdir();require_gui_source(self.app, str(other))
        before=self.state_bytes();self.app.on_toggle_operations()
        self.app.operations_tree.selection_set('1');self.root.update()
        text=str(self.app.operation_detail_label['text'])
        self.assertIn(source,text);self.assertIn(self.plans[0][1]['plan_id'],text)
        self.assertIn('移动 1',text);self.assertEqual(self.state_bytes(),before)
        self.app.on_undo();self.restored()

    def test_three_batches_can_undo_one_by_one(self):
        self.three()
        for head in (2,1,0):
            self.app.undo_button.invoke(); self.assertEqual(self.app.session.head,head)
            self.assertEqual(str(self.app.undo_button['state']), 'normal' if head else 'disabled')
        self.restored()

    def test_return_v1_undoes_v3_then_v2_and_preserves_v1_files(self):
        self.three(); original = gui.undo_plan; order=[]
        def tracked(settings, plan_id, **kw):
            order.append(plan_id); return original(settings,plan_id,**kw)
        with patch.object(gui,'undo_plan',side_effect=tracked):self.app.on_return_to(1)
        self.assertEqual(order,[self.plans[2][1]['plan_id'],self.plans[1][1]['plan_id']])
        self.assertFalse((self.sandbox.downloads/'first.pdf').exists())
        self.assertTrue((self.sandbox.downloads/'second.pdf').exists())
        self.assertEqual(self.app.session.head,1)
        self.app.on_return_to(0); self.restored()

    def test_direct_return_v0_and_plan_confirmation_bytes_immutable(self):
        self.three(); before={p:b for p,b in self.state_bytes().items() if p.startswith(('plans/','confirmations/'))}
        self.app.on_return_to(0); self.restored()
        self.assertEqual({p:self.state_bytes()[p] for p in before},before)
        self.assertTrue(all(s.status=='undone' for s in self.app.session.steps))

    def test_cross_source_chain_undo_uses_each_original_settings(self):
        self.build(); self.batch('a.pdf')
        other=self.sandbox.root/'OtherSource'; other.mkdir(); require_gui_source(self.app, str(other)); self.batch('b.pdf',other)
        require_gui_source(self.app, str(self.sandbox.downloads)); self.batch('c.pdf')
        self.assertEqual([s.settings.downloads_dir for s in self.app.session.steps],
            [str(self.sandbox.downloads),str(other),str(self.sandbox.downloads)])
        before_source=self.app.settings
        self.app.on_return_to(0); self.restored(); self.assertIs(self.app.settings,before_source)

    def test_cancel_return_zero_moves_and_does_not_write_records(self):
        self.three(); before=self.state_bytes(); head=self.app.session.head
        with patch.object(gui.messagebox,'askokcancel',return_value=False):self.app.on_return_to(0)
        self.assertEqual(self.state_bytes(),before); self.assertEqual(self.app.session.head,head)
        self.app.on_return_to(0); self.restored()

    def test_reentrant_calls_cannot_organize_undo_preview_or_switch_sources(self):
        self.three(); real=gui.undo_plan; order=[]
        other=self.sandbox.root/'OtherSource'; other.mkdir()
        def nested(settings,pid,**kw):
            plan=self.app.plan; current=self.app.settings
            self.app.on_preview(); self.app.on_organize(); self.app.on_undo(); self.app.on_return_to(0)
            self.assertFalse(self.app.select_source(str(other)))
            self.assertIs(self.app.plan,plan); self.assertIs(self.app.settings,current)
            order.append(pid); return real(settings,pid,**kw)
        with patch.object(gui,'undo_plan',side_effect=nested):self.app.on_return_to(0)
        self.assertEqual(len(order),3); self.restored()
        self.app.on_return_to(0); self.assertEqual(len(order),3)

    def test_cancel_modal_blocks_source_switch_without_consuming_versions(self):
        self.three(); other=self.sandbox.root/'OtherSource'; other.mkdir(); before=self.state_bytes()
        def cancel(*args,**kwargs):
            self.assertFalse(self.app.select_source(str(other)))
            self.app.on_organize(); return False
        with patch.object(gui.messagebox,'askokcancel',side_effect=cancel):self.app.on_return_to(0)
        self.assertEqual(self.state_bytes(),before); self.assertEqual(self.app.session.head,3)
        self.app.on_return_to(0); self.restored()

    def test_original_location_occupied_stops_earlier_batches_and_preserves_blocked_record(self):
        self.three(); path=self.sandbox.downloads/'third.pdf'; path.write_bytes(b'EXTERNAL')
        self.app.on_return_to(0)
        self.assertEqual(self.app.session.head,3); self.assertEqual(self.app.session.steps[-1].status,'needs_review')
        self.assertFalse((self.sandbox.downloads/'second.pdf').exists())
        self.assertIn('未退回',self.app.problem_label['text']); self.assertEqual(path.read_bytes(),b'EXTERNAL')
        blocked = read_snapshot(self.app.settings, self.plans[-1][1]['plan_id'])
        path.unlink(); self.app.on_return_to(0)
        self.assertEqual(self.app.session.head,3)
        self.assertEqual(read_snapshot(self.app.settings, self.plans[-1][1]['plan_id'])['entries'],blocked['entries'])
        self.assertFalse((self.sandbox.downloads/'first.pdf').exists())

    def test_modified_destination_stops_chain_without_overwriting_external_change(self):
        self.three(); dest=Path(self.plans[-1][1]['operations'][0]['dest']); dest.write_bytes(b'EXTERNAL CHANGE')
        self.app.on_return_to(0)
        self.assertEqual(self.app.session.head,3); self.assertEqual(dest.read_bytes(),b'EXTERNAL CHANGE')
        self.assertFalse((self.sandbox.downloads/'first.pdf').exists())
        self.assertIn('未退回',self.app.problem_label['text'])

    def test_corrupted_journal_stops_chain_and_preserves_corrupted_bytes(self):
        self.three(); pid=self.plans[-1][1]['plan_id']
        path=next((self.sandbox.state/'journals').glob(f'journal-{pid}.gen-*.jsonl'))
        path.write_bytes(path.read_bytes()+b'{bad journal}\n'); old=path.read_bytes()
        self.app.on_return_to(0)
        self.assertEqual(self.app.session.head,3); self.assertEqual(path.read_bytes(),old)
        self.assertFalse((self.sandbox.downloads/'first.pdf').exists())
        self.assertIn('未退回',self.app.problem_label['text'])

    def test_lock_occupied_stops_chain_then_retries_after_release(self):
        self.three(); lock=GlobalLock(self.app.settings); lock.acquire(kind='test',action='hold')
        try:
            self.app.on_return_to(0); self.assertEqual(self.app.session.head,3)
            self.assertFalse((self.sandbox.downloads/'third.pdf').exists())
        finally:lock.release()
        self.app.on_return_to(0); self.restored()

    def test_partial_execution_with_actual_move_is_a_version(self):
        self.build(); self.seed('a.pdf'); self.seed('b.pdf'); self.app.on_preview()
        plan=copy.deepcopy(self.app.plan); self.plans.append((self.app.settings,plan))
        real=AtomicMover.rename_excl_at; calls=[0]
        def one_conflict(mover,*a,**kw):
            calls[0]+=1
            if calls[0]==2:raise DestinationExists('ARTIFICIAL conflict')
            return real(mover,*a,**kw)
        with patch.object(AtomicMover,'rename_excl_at',one_conflict):self.app.on_organize()
        step=self.app.session.active[-1]
        self.assertEqual(step.moved,1); self.assertEqual(step.skipped,1)
        self.assertIn('跳过 1',self.app.problem_label['text'])
        self.assertIn('部分整理',self.app.operations_tree.item(str(step.version),'values')[2])
        self.app.on_undo(); self.restored()

    def test_partial_undo_stops_after_newer_batch_then_preserves_older_batch(self):
        self.three(); occupied=self.sandbox.downloads/'second.pdf'; occupied.write_bytes(b'EXTERNAL')
        self.app.on_return_to(0)
        self.assertEqual(self.app.session.head,2)
        self.assertEqual(self.app.session.steps[2].status,'undone')
        self.assertFalse((self.sandbox.downloads/'first.pdf').exists())
        occupied.unlink(); self.app.on_return_to(0)
        self.assertEqual(self.app.session.head,2)
        self.assertFalse((self.sandbox.downloads/'first.pdf').exists())
        self.assertEqual((self.sandbox.downloads/'third.pdf').read_bytes(),self.originals[str(self.sandbox.downloads/'third.pdf')][1])

    def test_undo_then_organize_creates_branch_without_reusing_old_version(self):
        self.three(); self.app.on_return_to(1); old=self.state_bytes()
        self.app.on_preview(); self.app.on_organize(); self.plans.append((self.app.settings,copy.deepcopy(self.app.plan)))
        step=self.app.session.active[-1]
        self.assertEqual((step.version,step.parent),(4,1))
        self.assertEqual([s.version for s in self.app.session.active],[1,4])
        for key,data in old.items():
            if key.startswith(('plans/','confirmations/','journals/')):self.assertEqual(self.state_bytes()[key],data)
        before=self.state_bytes(); self.app.on_return_to(2); self.assertEqual(self.state_bytes(),before)
        self.app.on_return_to(0); self.restored()

    def test_reopen_session_empty_but_persistent_undo_remains_available(self):
        self.build(); self.batch('a.pdf'); old=self.state_bytes(); self.app.on_close()
        app=self.build(selected=False)
        self.assertEqual(app.session.steps,[]); self.assertIsNone(app.plan)
        self.assertEqual(self.state_bytes(),old)
        require_gui_source(app, str(self.sandbox.downloads)); self.assertEqual(app.session.steps,[])
        self.assertIsNotNone(app.plan); app.on_undo(); self.restored()

    def test_viewing_records_settings_and_preview_does_not_create_versions(self):
        app=self.build(); self.seed('a.pdf'); app.on_preview(); before=self.state_bytes()
        app.on_toggle_operations(); app.on_toggle_operations(); app.on_toggle_settings(); app.on_toggle_settings()
        self.assertEqual(app.session.steps,[]); self.assertEqual(self.state_bytes(),before)
        app.on_preview(); self.assertEqual(app.session.steps,[])

    def test_waiting_intent_is_tracked_even_without_complete_execution_result(self):
        self.build(); self.seed('a.pdf'); self.app.on_preview(); self.app.on_confirm()
        settings=self.app.settings; pid=self.app.plan['plan_id']
        class Interrupted(BaseException):pass
        real = AtomicMover.rename_excl_at
        def stop(mover,*args,**kwargs):
            real(mover,*args,**kwargs)
            raise Interrupted()
        with patch.object(AtomicMover,'rename_excl_at',stop), self.assertRaises(Interrupted):
            self.app.perform_execution()
        self.assertEqual(len(self.app.session.active),1)
        self.assertEqual(self.app.session.active[0].uncertain,1)
        self.assertFalse((self.sandbox.downloads/'a.pdf').exists())
        self.app.on_recover(); self.app.on_undo()
        self.assertTrue((self.sandbox.downloads/'a.pdf').exists())
        self.assertEqual(lstat_identity(str(self.sandbox.downloads/'a.pdf')).as_dict(),self.originals[str(self.sandbox.downloads/'a.pdf')][0])


class WorkflowLifecycleTests(WorkflowBase):
    def test_source_actions_results_and_records_fit_minimum_and_large_windows(self):
        app=self.build(selected=False); self.seed('a.pdf'); self.seed('Unselected/b.pdf')
        self.root.deiconify()
        for stage in ('initial','preview','organized','undone'):
            if stage=='preview':require_gui_source(app, str(self.sandbox.downloads));require_gui_preview(app)
            elif stage=='organized':app.on_organize()
            elif stage=='undone':app.on_undo()
            for size in ('900x720','1100x760','1400x900'):
                self.root.geometry(size+'+3000+3000');self.root.update()
                for widget in (app.source_button,app.source_label,app.scope_label,app.tree,app.preview_button,
                               app.organize_button,app.undo_button,app.operations_button,app.daily_status_label):
                    self.assertTrue(widget.winfo_viewable())
                    node=widget
                    while node is not self.root:
                        parent=node.master
                        self.assertGreaterEqual(node.winfo_rootx()-parent.winfo_rootx(),0)
                        self.assertGreaterEqual(node.winfo_rooty()-parent.winfo_rooty(),0)
                        self.assertLessEqual(node.winfo_rootx()-parent.winfo_rootx()+node.winfo_width(),parent.winfo_width())
                        self.assertLessEqual(node.winfo_rooty()-parent.winfo_rooty()+node.winfo_height(),parent.winfo_height())
                        node=parent
            app.on_toggle_operations()
            for size in ('720x560','900x720'):
                app.operations_window.geometry(size+'+3000+3000');self.root.update()
                if app.session.steps:
                    app.operations_tree.selection_set('1');self.root.update()
                for button in (app.return_button,app.return_zero_button,app.step_recover_button):
                    self.assertTrue(button.winfo_viewable())
                    self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),app.operations_window.winfo_rooty()+app.operations_window.winfo_height())
                    self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),app.operations_window.winfo_rootx()+app.operations_window.winfo_width())
                self.assertLessEqual(int(app.operation_detail_label['wraplength']),app.operations_window.winfo_width()-24)
            app.on_toggle_operations()

    def test_close_during_slow_discovery_cleans_only_owned_windows_and_no_versions(self):
        app=self.build();started=threading.Event();release=threading.Event();real=gui.NewFileWatcher.check_once
        def slow(watcher,**opts):
            started.set();release.wait(3);return real(watcher,**opts)
        with patch.object(gui.NewFileWatcher,'check_once',slow):
            app.on_check_now();self.assertTrue(started.wait(1));worker=app._discovery_thread
            app.on_close();release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertTrue(app._closing)
        self.assertEqual(app.session.steps,[]);self.app=None

    def test_real_read_failure_is_explained_without_claiming_full_coverage(self):
        self.seed('Chosen/a.pdf');app=self.build();app.selected_scope=['Chosen'];app.subfolder_var.set(True)
        app.on_toggle_subfolders();real=os.scandir;denied=str(self.sandbox.downloads/'Chosen')
        def fail(path):
            if path==denied:raise PermissionError('ARTIFICIAL read failure')
            return real(path)
        with patch('doc_selecter.scanner.os.scandir',fail):app.on_preview()
        self.assertEqual(len(app.plan['operations']),0)
        self.assertIn('递归读取失败/扫描变化',app.remaining_label['text'])
        self.assertTrue(app.problem_label['text']);self.assertEqual(str(app.organize_button['state']),'disabled')

class SessionWarningTests(WorkflowBase):
    def test_blocked_v2_warning_retains_original_source_after_switch_and_new_preview(self):
        self.three(); app = self.app; original = app.settings
        occupied = self.sandbox.downloads / 'second.pdf'; occupied.write_bytes(b'EXTERNAL OCCUPANT')
        app.on_return_to(0)
        self.assertEqual(app.session.head, 2)
        self.assertEqual(app.session.steps[2].status, 'undone')
        warning = app.problem_label['text']
        self.assertIn('本次会话 V2', warning)
        self.assertIn(f'原来源：{original.downloads_dir}', warning)
        self.assertIn('本次确定已还原 1 个', warning)
        self.assertEqual(occupied.read_bytes(), b'EXTERNAL OCCUPANT')
        clean = self.sandbox.root / 'Clean'; clean.mkdir()
        require_gui_source(app, str(clean)); require_gui_preview(app)
        self.assertEqual(app.problem_label['text'], warning)
        self.assertIn('Clean', app.source_label['text'])
        self.assertNotIn('受阻', app.daily_status_label['text'])
        self.assertEqual(app.plan['downloads_dir'], str(clean))
        self.assertEqual(app.session.active[-1].settings, original)
        self.root.geometry('900x720+3000+3000'); self.root.deiconify(); self.root.update()
        for widget in (app.source_label, app.problem_label, app.preview_button, app.organize_button, app.operations_button):
            self.assertTrue(widget.winfo_viewable())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())
        before = self.state_bytes(); app.on_toggle_operations()
        app.operations_tree.selection_set('2'); app.on_describe_operation()
        self.assertIn(original.downloads_dir, app.operation_detail_label['text'])
        self.assertEqual(str(app.step_recover_button['state']), 'normal')
        self.assertEqual(self.state_bytes(), before)

    def test_identically_named_sources_are_distinguished_by_original_path(self):
        app = self.build()
        first = self.sandbox.root / 'First' / 'Shared'
        second = self.sandbox.root / 'Second' / 'Shared'
        first.mkdir(parents=True); second.mkdir(parents=True)
        require_gui_source(app, str(first)); self.batch('first.pdf', first)
        require_gui_source(app, str(second)); self.batch('second.pdf', second)
        occupied = second / 'second.pdf'; occupied.write_bytes(b'EXTERNAL OCCUPANT')
        app.on_return_to(0)
        self.assertEqual(app.session.head, 2)
        require_gui_source(app, str(first)); require_gui_preview(app)
        self.assertIn(f'原来源：{second}', app.problem_label['text'])
        self.assertNotIn(f'原来源：{first}', app.problem_label['text'])
        self.assertEqual(app.settings.downloads_dir, str(first))
        self.assertEqual(occupied.read_bytes(), b'EXTERNAL OCCUPANT')
        self.assertFalse((first / 'first.pdf').exists())

    def test_legacy_sensitive_source_warning_redacts_names_without_path_metadata(self):
        self.three(); app = self.app; step = app.session.active[-1]
        # Historical presentation fixture only. The report is supplied to the real
        # GUI callback; this test does not claim an undo for the synthetic root.
        sensitive = str(self.sandbox.root / 'ssh' / '合同资料' / 'Shared')
        step.settings = copy.copy(step.settings)
        object.__setattr__(step.settings, 'downloads_dir', sensitive)
        original_stat = os.stat; original_lstat = os.lstat
        def guarded(fn):
            def call(path, *args, **kwargs):
                if isinstance(path, (str, bytes, os.PathLike)):
                    self.assertFalse(os.fsdecode(path).startswith(sensitive))
                return fn(path, *args, **kwargs)
            return call
        before = self.state_bytes()
        report = ReturnReport(target=0, stopped_at=3, detail='本批未完整还原')
        with patch.object(app.session, 'return_to', return_value=report), \
                patch('os.stat', side_effect=guarded(original_stat)), patch('os.lstat', side_effect=guarded(original_lstat)):
            app.on_return_to(0)
        warning = app.problem_label['text']
        self.assertIn('本次会话 V3', warning)
        self.assertIn('原来源：', warning)
        self.assertIn('敏感信息已隐藏', warning)
        self.assertNotIn('/ssh/', warning); self.assertNotIn('合同资料', warning)
        self.assertEqual(app.session.head, 3)
        self.assertEqual(self.state_bytes(), before)

    def test_successful_return_clears_only_resolved_session_warning(self):
        self.three(); app = self.app; lock = GlobalLock(app.settings)
        lock.acquire(kind='test', action='hold')
        try:
            app.on_return_to(0)
            self.assertIn('本次会话 V3', app.problem_label['text'])
        finally:
            lock.release()
        app.on_return_to(0); self.restored()
        self.assertEqual(app.session.head, 0)
        self.assertEqual(app._chain_problem, '')
        self.assertNotIn('本次会话', app.problem_label['text'])
        require_gui_preview(app)
        self.assertNotIn('本次会话', app.problem_label['text'])


class FixtureDiagnosticTests(WorkflowBase):
    def test_positive_fixture_name_has_only_fixed_words_and_short_hex_groups(self):
        from doc_selecter.rules import is_sensitive_dir
        self.assertRegex(self.sandbox.root.name, r'^docsel-test-(?:[a-f0-9]{4}-){7}[a-f0-9]{4}$')
        self.assertFalse(is_sensitive_dir(self.sandbox.root.name))
        require_gui_source(self.build(selected=False), str(self.sandbox.downloads))

    def test_sensitive_fixture_refusal_is_diagnosed_before_preview_without_moving(self):
        app = self.build(selected=False)
        source = self.sandbox.root / 'docsel-test-ssh' / 'Downloads'
        source.mkdir(parents=True)
        path = source / 'ordinary.pdf'; path.write_bytes(b'ARTIFICIAL')
        identity = lstat_identity(str(path)); before = self.state_bytes()
        with patch.object(app, 'on_preview', wraps=app.on_preview) as preview:
            with self.assertRaisesRegex(AssertionError, 'GUI前置失败：来源选择未成功') as raised:
                require_gui_source(app, str(source))
            preview.assert_not_called()
        self.assertIn(str(source), str(raised.exception))
        self.assertIn('拒绝提示=', str(raised.exception))
        self.assertIn('尚未选择', str(raised.exception))
        self.assertIsNone(app.plan)
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(lstat_identity(str(path)), identity)
        self.assertEqual(path.read_bytes(), b'ARTIFICIAL')

    def test_failed_preview_reports_product_error_instead_of_none_plan(self):
        app = self.build(); self.seed('ordinary.pdf'); before = self.state_bytes()
        with patch.object(gui, 'write_plan', side_effect=OSError('ARTIFICIAL save refused')):
            with self.assertRaisesRegex(AssertionError, '预览未成功生成并保存') as raised:
                require_gui_preview(app)
        self.assertIn('ARTIFICIAL save refused', str(raised.exception))
        self.assertFalse(app._preview_ready)
        self.assertIsNone(app.plan)
        self.assertEqual(self.state_bytes(), before)
