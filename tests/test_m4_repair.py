"""R1–R8 主控反例回归；只使用人工沙盒。"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.harness import SandboxTestCase, require_gui_source
from doc_selecter.executor import execute_plan, recover_execution
from doc_selecter.planner import PlanError, build_plan, verify_plan_binding, write_plan, write_confirmation
from doc_selecter.plan_hash import compute_plan_hash
from doc_selecter.scanner import scan_scope, list_selectable_folders, collect_temp_files
from doc_selecter.watcher import NewFileWatcher
from doc_selecter.undo import undo_plan, UndoRefused


class ScopePrivacyRepairTests(SandboxTestCase):
    def test_deep_selection_cli_binding_and_roundtrip(self):
        for name in ['top.pdf', 'A/chosen/allowed.pdf', 'A/root.txt', 'A/sibling/keep.png']:
            self.sandbox.add_file(name)
        p = self.sandbox.make_plan(scope_dirs=['A/chosen'], recursive=True)
        self.assertEqual([o['rel_path'] for o in p['operations']], ['A/chosen/allowed.pdf', 'top.pdf'])
        self.assertEqual(p['selected_scope'], ['', 'A/chosen'])
        self.assertNotIn('A', p['scan_scope'])
        self.assertEqual(execute_plan(self.sandbox.settings, p['plan_id'], p['plan_hash']).moved, 2)
        self.assertEqual(undo_plan(self.sandbox.settings, p['plan_id'], confirm=True).restored, 2)
        self.assertTrue((self.sandbox.downloads / 'A/root.txt').is_file())
        self.assertTrue((self.sandbox.downloads / 'A/sibling/keep.png').is_file())

    def test_explicit_roots_hash_and_binding_reject_transit_file(self):
        self.sandbox.add_file('A/chosen/a.pdf')
        self.sandbox.add_file('A/other.pdf')
        p = self.sandbox.make_plan(scope_dirs=['A/chosen'], recursive=True)
        wide = self.sandbox.make_plan(scope_dirs=['A'], recursive=True)
        self.assertNotEqual(p['plan_hash'], wide['plan_hash'])
        altered = copy.deepcopy(p)
        altered['selected_scope'] = ['', 'A']
        self.assertNotEqual(compute_plan_hash(altered), p['plan_hash'])
        altered = copy.deepcopy(p)
        altered['operations'] = wide['operations']
        altered['scan_scope'] = wide['scan_scope']
        with self.assertRaises(PlanError):
            verify_plan_binding(self.sandbox.settings, altered)

    def _record(self, callback):
        visited = []
        original = os.scandir
        def record(path):
            visited.append(Path(path).relative_to(self.sandbox.downloads).as_posix())
            return original(path)
        with patch('doc_selecter.scanner.os.scandir', side_effect=record):
            result = callback()
        return result, visited

    def test_default_auxiliary_scan_never_enters_children(self):
        for name in ['ordinary/x.pdf', '.hidden/x.pdf', 'App.app/x.pdf', 'Code/package.json',
                     'password_backup/x.pdf', '下载分类/文档资料/x.pdf']:
            self.sandbox.add_file(name)
        _, reads = self._record(lambda: scan_scope(str(self.sandbox.downloads), target_parent_name='下载分类'))
        self.assertTrue(reads)
        self.assertEqual(set(reads), {'.'})

    def test_picker_counting_stops_before_forbidden_descendants(self):
        for name in ['Normal/visible.pdf', 'Normal/password_backup/x.pdf', 'Normal/App.app/x.pdf',
                     'Normal/.hidden/x.pdf', 'Normal/Code/package.json', 'Normal/Code/Sub/x.pdf']:
            self.sandbox.add_file(name)
        folders, reads = self._record(lambda: list_selectable_folders(
            str(self.sandbox.downloads), target_parent_name='下载分类'))
        self.assertEqual([(f.rel_path, f.file_count) for f in folders], [('Normal', 1)])
        self.assertFalse(any(x.startswith(('Normal/password_backup', 'Normal/App.app', 'Normal/.hidden',
                                          'Normal/Code/Sub')) for x in reads))
        self.assertIn('Normal/Code', reads)  # 仅直接标记名字识别允许

    def test_deep_selection_auxiliary_reads_no_sibling(self):
        self.sandbox.add_file('A/chosen/a.pdf')
        self.sandbox.add_file('A/sibling/x.pdf')
        _, reads = self._record(lambda: scan_scope(str(self.sandbox.downloads),
            target_parent_name='下载分类', scope_dirs=['A/chosen'], recursive=True))
        self.assertNotIn('A/sibling', reads)

    def test_unselected_sensitive_never_persisted_or_reported(self):
        name = 'password_backup'
        self.sandbox.add_file(name + '/x.pdf')
        self.sandbox.add_file('Chosen/a.pdf')
        for recursive, roots in [(False, []), (True, ['Chosen']), (True, [name])]:
            p = self.sandbox.make_plan(scope_dirs=roots, recursive=recursive, confirm=False)
            self.assertEqual(p['counts']['sensitive_skipped'], 1)
            self.assertNotIn(name, self.sandbox.plan_file(p['plan_id']).read_text())
            scan = scan_scope(str(self.sandbox.downloads), target_parent_name='下载分类',
                              scope_dirs=roots, recursive=recursive)
            self.assertNotIn(name, repr(scan.added_during_scan) + repr(scan.removed_during_scan))

    def test_sensitive_stat_failure_redacts_name(self):
        name = 'password_backup'
        self.sandbox.add_file(name + '/x.pdf')
        original = os.scandir
        class Entry:
            def __init__(self, item): self.name = item.name
            def stat(self, **kw): raise PermissionError(name)
        class Entries:
            def __enter__(self): return iter([Entry(type('E', (), {'name': name})())])
            def __exit__(self, *args): return False
        with patch('doc_selecter.scanner.os.scandir', return_value=Entries()):
            scan = scan_scope(str(self.sandbox.downloads), target_parent_name='下载分类')
        self.assertEqual(scan.sensitive_count, 1)
        self.assertNotIn(name, repr(scan.excluded) + repr(scan.scan_issues) + repr(scan.metadata_failures))


class DestinationChainRepairTests(SandboxTestCase):
    def _plan(self):
        self.sandbox.add_file('A/B/file.pdf')
        self.sandbox.add_dir('下载分类/文档资料/A/B')
        return self.sandbox.make_plan(scope_dirs=['A'], recursive=True)

    def _replace(self):
        a = self.sandbox.downloads / '下载分类/文档资料/A'
        old = a.with_name('previous-A')
        a.rename(old); a.mkdir(); (old / 'B').rename(a / 'B')

    def _logs(self):
        return {str(p.relative_to(self.sandbox.state)): p.read_bytes()
                for p in self.sandbox.state.rglob('*') if p.is_file() and
                (p.suffix == '.jsonl' or 'manifest' in p.name)}

    def test_execute_rejects_replaced_ancestor_same_leaf_inode(self):
        p = self._plan()
        self.assertEqual(set(p['dir_identities']['dest_dirs']), {'文档资料', '文档资料/A', '文档资料/A/B'})
        leaf = (self.sandbox.downloads / '下载分类/文档资料/A/B').stat().st_ino
        self._replace()
        self.assertEqual((self.sandbox.downloads / '下载分类/文档资料/A/B').stat().st_ino, leaf)
        before = self._logs()
        with self.assertRaises(PlanError): execute_plan(self.sandbox.settings, p['plan_id'], p['plan_hash'])
        self.assertEqual(before, self._logs())
        self.assertTrue((self.sandbox.downloads / 'A/B/file.pdf').is_file())

    def test_undo_rejects_replaced_ancestor_before_log_write(self):
        p = self._plan()
        self.assertEqual(execute_plan(self.sandbox.settings, p['plan_id'], p['plan_hash']).moved, 1)
        self._replace(); before = self._logs()
        with self.assertRaises(UndoRefused): undo_plan(self.sandbox.settings, p['plan_id'], confirm=True)
        self.assertEqual(before, self._logs())
        self.assertFalse((self.sandbox.downloads / 'A/B/file.pdf').exists())

    def test_recover_rejects_replaced_ancestor_before_repair(self):
        p = self._plan()
        self.assertEqual(execute_plan(self.sandbox.settings, p['plan_id'], p['plan_hash']).moved, 1)
        self._replace(); before = self._logs()
        with self.assertRaises(PlanError): recover_execution(self.sandbox.settings, p['plan_id'])
        self.assertEqual(before, self._logs())

    def test_recover_pending_intent_rejects_before_supplement(self):
        from doc_selecter.atomic_move import AtomicMover
        p = self._plan()
        original = AtomicMover.rename_excl_at
        class Interrupted(BaseException): pass
        def interrupt(mover, *args, **kwargs):
            original(mover, *args, **kwargs)
            raise Interrupted()
        with patch.object(AtomicMover, 'rename_excl_at', interrupt):
            with self.assertRaises(Interrupted):
                execute_plan(self.sandbox.settings, p['plan_id'], p['plan_hash'])
        self._replace(); before = self._logs()
        with self.assertRaises(PlanError): recover_execution(self.sandbox.settings, p['plan_id'])
        self.assertEqual(before, self._logs())

    def test_existing_target_missing_nested_dirs_can_create_and_undo(self):
        self.sandbox.add_file('A/B/file.pdf')
        self.sandbox.add_file('A/C/file.pdf')
        self.sandbox.add_dir('下载分类/文档资料')
        p = self.sandbox.make_plan(scope_dirs=['A'], recursive=True)
        self.assertEqual(execute_plan(self.sandbox.settings, p['plan_id'], p['plan_hash']).moved, 2)
        self.assertEqual(undo_plan(self.sandbox.settings, p['plan_id'], confirm=True).restored, 2)


class WatcherRepairTests(SandboxTestCase):
    def _check(self, watcher, **kw):
        return watcher.check_once(downloads_dir=str(self.sandbox.downloads), target_parent_name='下载分类', **kw)

    def test_partial_permission_failure_not_reported_empty(self):
        self.sandbox.add_file('Chosen/a.pdf'); self.sandbox.add_file('visible.pdf')
        self.sandbox.add_file('password_backup/private.pdf')
        original = os.scandir
        def fail(path):
            if Path(path) == self.sandbox.downloads / 'Chosen':
                raise PermissionError('password_backup must not leak from exception')
            return original(path)
        watcher = NewFileWatcher(); watcher.enable()
        with patch('doc_selecter.scanner.os.scandir', side_effect=fail):
            r = self._check(watcher, scope_dirs=['Chosen'], recursive=True)
            issues = []
            collect_temp_files(str(self.sandbox.downloads), target_parent_name='下载分类',
                               scope_dirs=['Chosen'], recursive=True, issues=issues)
        self.assertTrue(r.error); self.assertTrue(r.partial_coverage); self.assertTrue(r.scan_issues)
        self.assertTrue(issues); self.assertEqual(r.total_seen, 1)
        text = watcher.format_report(r)
        self.assertNotIn('未发现新的', text); self.assertNotIn('password_backup', text)

    def test_deep_watcher_scope_matches_plan(self):
        for name in ['A/chosen/a.pdf', 'A/root.pdf', 'A/sibling/x.pdf']: self.sandbox.add_file(name)
        watcher = NewFileWatcher(); watcher.enable()
        self.assertEqual(self._check(watcher, scope_dirs=['A/chosen'], recursive=True).settling, ['A/chosen/a.pdf'])

    def _many(self, count):
        for i in range(count): self.sandbox.add_file(f'{i:03}.pdf')
        now = [0]
        w = NewFileWatcher(clock=lambda: now[0]); w.enable()
        self._check(w); now[0] = 10
        reports = [self._check(w) for _ in range((count + 49) // 50)]
        self.assertEqual(len(set(x for r in reports for x in r.new_files)), count)
        self.assertEqual([r.total_new for r in reports], list(range(count, 0, -50)))
        self.assertTrue(all(r.total_seen == count for r in reports))
        self.assertEqual(self._check(w).total_new, 0)
        p = self.sandbox.make_plan(confirm=False)
        self.assertEqual(len(p['operations']), count)

    def test_51_new_files_no_loss(self): self._many(51)
    def test_123_new_files_no_loss(self): self._many(123)


class GuiCategoryRepairTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        from tests.test_gui import _SHARED_ROOT
        from doc_selecter import gui
        import tkinter as tk
        self.root = tk.Toplevel(_SHARED_ROOT); self.root.withdraw()
        self.addCleanup(self.root.destroy)
        for name in ['showinfo', 'showerror', 'showwarning']:
            mock = self.enterContext(patch.object(gui.messagebox, name))
        self.app = gui.App(self.root, self.sandbox.settings)
        require_gui_source(self.app, self.sandbox.settings.downloads_dir)

    def test_actual_toggle_reconfirm_executes_new_collection(self):
        self.sandbox.add_file('a.pdf'); self.sandbox.add_file('b.png')
        a = self.app; a.on_preview(); a.on_confirm()
        old = a.plan; old_bytes = self.sandbox.plan_file(old['plan_id']).read_bytes()
        a._selection_vars['图片截图'].set(False); a.on_toggle_category()
        self.assertIsNone(a.confirmed_hash); self.assertNotEqual(old['plan_id'], a.plan['plan_id'])
        self.assertNotIn('图片截图', a.plan['category_selection'])
        a.on_confirm(); self.assertEqual(a.perform_execution().moved, 1)
        self.assertTrue((self.sandbox.downloads / 'b.png').is_file())
        self.assertEqual(self.sandbox.plan_file(old['plan_id']).read_bytes(), old_bytes)
        self.assertEqual(undo_plan(self.sandbox.settings, a.plan['plan_id'], confirm=True).restored, 1)

    def test_cancel_all_categories_never_falls_back(self):
        self.sandbox.add_file('a.pdf'); a = self.app; a.on_preview()
        for var in a._selection_vars.values(): var.set(False)
        a.on_toggle_category(); a.on_confirm()
        self.assertEqual(a.plan['category_selection'], []); self.assertEqual(a.plan['operations'], [])
        self.assertEqual(a.perform_execution().moved, 0)
        self.assertTrue((self.sandbox.downloads / 'a.pdf').is_file())

    def test_enabling_watch_rejects_prior_disabled_worker_result(self):
        from unittest.mock import Mock
        from doc_selecter.watcher import DiscoveryReport
        a = self.app
        old = copy.copy(a.watcher)
        token = a._discovery_generation
        a._discovery_thread = Mock(is_alive=lambda: False)
        a._discovery_queue.put((token, old, DiscoveryReport(), None))
        a._watch_enabled_var.set(True); a.on_toggle_watch()
        a._poll_discovery()
        self.assertTrue(a.watcher.enabled)
        self.assertIsNone(a.watcher.last_report)
        a._watch_enabled_var.set(False); a.on_toggle_watch()

    def test_confirm_detects_checkbox_change_without_callback(self):
        self.sandbox.add_file('a.pdf'); self.sandbox.add_file('b.png')
        a = self.app; a.on_preview(); old = a.plan['plan_id']
        a._selection_vars['图片截图'].set(False); a.on_confirm()
        self.assertNotEqual(old, a.plan['plan_id'])
        self.assertEqual(a.perform_execution().moved, 1)
        self.assertTrue((self.sandbox.downloads / 'b.png').is_file())


class GuiSlowLifecycleRepairTests(SandboxTestCase):
    def test_slow_scan_event_close_scope_switch_and_no_task_pileup(self):
        self.sandbox.add_file('a.pdf')
        driver = r'''
import json, os, threading, time, tkinter as tk
from unittest.mock import patch
from doc_selecter.gui import App
from tests.harness import require_gui_source
from doc_selecter.config import load_settings
root=tk.Tk(); root.withdraw(); app=App(root, load_settings())
require_gui_source(app, app.settings.downloads_dir)
app.watcher.enable(); app.watcher.set_settle_seconds(0)
original=os.scandir; started=threading.Event(); release=threading.Event(); calls=[]
main=threading.get_ident(); updates=[]; actual=app._set_watch_text
app._set_watch_text=lambda text: (updates.append((threading.get_ident(), text)), actual(text))
def slow(path):
    calls.append(threading.get_ident()); started.set(); release.wait(2)
    return original(path)
result={}; workers=[]; start=time.monotonic()
def heartbeat():
    result['heartbeat_ms']=(time.monotonic()-start)*1000
    assert started.is_set()
    worker=app._discovery_thread; workers.append(worker)
    for _ in range(20): app.on_check_now()
    assert app._discovery_thread is worker
    app.selected_scope=['Other']; app._on_scope_changed()
    release.set(); root.after(60, switched)
def switched():
    assert app.watcher.last_report is None  # 旧范围结果未发布
    assert not workers[0].is_alive()
    assert app._discovery_thread is None
    result['stale_ignored']=True; result['single_worker']=True
    release.clear(); started.clear(); app.on_check_now()
    workers.append(app._discovery_thread)
    root.after(20, close)
def close():
    assert started.is_set()
    begin=time.monotonic(); app.on_close()
    result['close_ms']=(time.monotonic()-begin)*1000
    result['jobs_cancelled']=app.watch_job is None and app._discovery_poll_job is None
    release.set()
try:
    with patch('doc_selecter.scanner.os.scandir', side_effect=slow):
        root.after(0, app.on_check_now); root.after(20, heartbeat)
        root.after(4000, app.on_close); root.mainloop()
finally:
    app.on_close(); release.set()
    for worker in workers: worker.join(2)
result['workers_finished']=all(not w.is_alive() for w in workers)
result['tk_updates_main_only']=all(tid==main for tid,_ in updates)
result['io_off_main']=all(tid!=main for tid in calls)
assert result['heartbeat_ms']<180 and result['close_ms']<180, result
assert result['workers_finished'] and result['jobs_cancelled']
assert result['tk_updates_main_only'] and result['io_off_main']
print(json.dumps(result))
'''
        proc = subprocess.run([sys.executable, '-c', driver], capture_output=True, text=True,
                              env=dict(os.environ, DOCSELECTER_TEST_ROOT=str(self.sandbox.root)), timeout=12)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(result['stale_ignored']); self.assertTrue(result['workers_finished'])
        self.assertLess(result['heartbeat_ms'], 180)
        evidence = os.environ.get('DOCSELECTER_REPAIR_EVIDENCE')
        if evidence:
            Path(evidence, 'gui-lifecycle.json').write_text(json.dumps(result, indent=2))
