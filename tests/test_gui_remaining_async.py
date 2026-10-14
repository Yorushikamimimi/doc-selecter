"""R14 real Tk/mainloop: bounded preparation, stale results and nonblocking close.

Modal answers are stubs, never human/native acceptance. Slow tests inject 1.5s
into an actual artificial-directory enumeration. 200ms tolerances are well below
that stall and accommodate ordinary local event scheduling, not slow I/O.
"""
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

from tests.harness import SandboxTestCase, require_gui_source
from tests import test_gui as gui_tests
from doc_selecter import gui, gui_remaining as remaining
from doc_selecter.atomic_move import dir_identity
from doc_selecter.planner import confirmation_path
from doc_selecter.journal import Journal

EVIDENCE = []


class RemainingAsyncTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(gui_tests.DISPLAY_AVAILABLE, gui_tests._DISPLAY_ERROR)
        self.calls = []
        self.main_thread = threading.get_ident()
        for name in ('showinfo', 'showwarning', 'showerror'):
            p = patch.object(gui.messagebox, name, side_effect=lambda *a, **kw: self.calls.append(a))
            p.start(); self.addCleanup(p.stop)
        p = patch.object(gui.messagebox, 'askokcancel', return_value=True)
        self.ask = p.start(); self.addCleanup(p.stop)
        self.root = gui.tk.Toplevel(gui_tests._SHARED_ROOT)
        self.app = gui.App(self.root, self.sandbox.settings)
        self.addCleanup(self.app.on_close)
        self.root.deiconify(); self.root.update()
        require_gui_source(self.app, str(self.sandbox.downloads))
        self.sandbox.add_file('a.pdf', b'ARTIFICIAL:a.pdf')
        self.sandbox.add_file('b.csv', b'ARTIFICIAL:b.csv')
        self.sandbox.add_dir('empty')

    def pump(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.root.update(); time.sleep(.002)
        self.assertTrue(predicate(), 'async test precondition timed out')

    def settled(self, d):
        self.pump(lambda: d._task is None and d.preparation.active is None)

    def open(self):
        self.app.remaining_button.invoke(); d = self.app._remaining_dialog
        self.settled(d)
        self.assertIsNotNone(d.discovery, (d.status['text'], self.calls))
        return d

    def preview(self, d):
        self.settled(d); d.all_button.invoke(); d.preview_button.invoke(); self.settled(d)
        self.assertIsNotNone(d.plan, (d.status['text'], self.calls))
        return d.plan

    def block(self, symbol):
        started, release = threading.Event(), threading.Event()
        original = getattr(remaining, symbol)
        def slow(*args, **kw):
            started.set()
            if not release.wait(5): raise TimeoutError('artificial task gate timed out')
            return original(*args, **kw)
        return started, release, patch.object(remaining, symbol, side_effect=slow)

    def unchanged(self):
        self.assertFileContent(self.sandbox.downloads/'a.pdf', b'ARTIFICIAL:a.pdf')
        self.assertFileContent(self.sandbox.downloads/'b.csv', b'ARTIFICIAL:b.csv')
        self.assertExists(self.sandbox.downloads/'empty')
        self.assertNotExists(self.sandbox.downloads/'下载分类')
        self.assertEqual(self.app.session.head, 0)

    def latency(self, kind, close_main=True):
        d = None if kind == 'first' else self.open()
        if kind in ('preview', 'save'):
            d.all_button.invoke()
        inode = os.lstat(self.sandbox.downloads).st_ino
        original_list = os.listdir; original_worker = remaining._prepare
        done = threading.Event(); injected = False; observations = {'kind': kind, 'delay_ms': 1500}
        ticks, callback_errors = [], []
        began = time.monotonic()
        def now(): return (time.monotonic()-began)*1000
        def slow_list(path):
            nonlocal injected
            if kind != 'save' and not injected and isinstance(path, int) and os.fstat(path).st_ino == inode:
                injected = True; observations['io_thread'] = threading.get_ident()
                observations['io_start_ms'] = now(); time.sleep(1.5); observations['io_end_ms'] = now()
            return original_list(path)
        def worker(*args):
            try: original_worker(*args)
            finally: done.set()
        original_write = remaining.write_plan
        def slow_save(*args):
            nonlocal injected
            injected = True; observations['io_thread'] = threading.get_ident()
            observations['io_start_ms'] = now(); time.sleep(1.5); observations['io_end_ms'] = now()
            return original_write(*args)
        def beat():
            ticks.append(now())
            if not self.app._closing: self.root.after(20, beat)
        def begin():
            observations['callback_start_ms'] = now()
            if kind == 'first': self.app.remaining_button.invoke()
            elif kind == 'refresh': d.refresh_button.invoke()
            else: d.preview_button.invoke()
            observations['callback_end_ms'] = now()
        def close():
            observations['close_ms'] = now()
            if close_main:
                self.app.on_close(); gui_tests._SHARED_ROOT.quit()
            else:
                d.close_button.invoke()
                self.assertFalse(d.window.winfo_exists())
                self.root.after(1550, finish)
        def finish():
            self.app.on_close(); gui_tests._SHARED_ROOT.quit()
        shared = gui_tests._SHARED_ROOT
        prior = shared.report_callback_exception
        shared.report_callback_exception = lambda *a: callback_errors.append(str(a))
        watchdog = shared.after(6000, lambda: shared.quit())
        self.root.after(20, beat); self.root.after(80, begin); self.root.after(200, close)
        try:
            with patch.object(remaining, '_prepare', side_effect=worker), patch('os.listdir', side_effect=slow_list), patch.object(remaining, 'write_plan', side_effect=slow_save if kind == 'save' else original_write):
                shared.mainloop()
                self.assertTrue(done.wait(3), 'worker did not complete after nonblocking close')
        finally:
            shared.after_cancel(watchdog); shared.report_callback_exception = prior
        observations.update(ticks_ms=ticks, max_heartbeat_gap_ms=max([b-a for a,b in zip(ticks,ticks[1:])]+[0]),
            close_lateness_ms=observations.get('close_ms', 6000)-200, callback_errors=callback_errors,
            modal_answers='stubbed, none needed for preparation', main_thread=self.main_thread)
        EVIDENCE.append(dict(test_id=self.id(), root=str(self.sandbox.root), **observations))
        self.assertTrue(injected)
        self.assertNotEqual(observations['io_thread'], self.main_thread)
        self.assertLess(observations['callback_end_ms']-observations['callback_start_ms'], 200)
        self.assertLess(observations['max_heartbeat_gap_ms'], 200)
        self.assertLess(observations['close_lateness_ms'], 200)
        self.assertEqual(callback_errors, []); self.assertEqual(self.calls, [])
        self.unchanged()
        self.assertIsNone(self.app._remaining_dialog.plan)
        if kind == 'preview': self.assertEqual(list((self.sandbox.state/'plans').glob('*.json')), [])
        if kind == 'save':
            paths = list((self.sandbox.state/'plans').glob('*.json'))
            self.assertEqual(len(paths), 1)
            p = json.loads(paths[0].read_text())
            self.assertNotExists(confirmation_path(self.app.settings, p['plan_id']))

    def test_first_discovery_slow_io_heartbeat_and_main_close(self): self.latency('first')
    def test_refresh_slow_io_heartbeat_and_main_close(self): self.latency('refresh')
    def test_preview_slow_io_heartbeat_and_main_close_before_save(self): self.latency('preview')
    def test_write_plan_slow_io_heartbeat_and_main_close_retains_started_save(self): self.latency('save')
    def test_dialog_close_during_slow_io_discards_late_completion(self): self.latency('refresh', False)

    def test_source_switch_during_discovery_discards_old_source(self):
        started, release, blocked = self.block('discover_remaining')
        with blocked:
            self.app.remaining_button.invoke(); d=self.app._remaining_dialog
            self.pump(started.is_set)
            other=self.sandbox.root/'Other'; other.mkdir(); (other/'other.pdf').write_bytes(b'ARTIFICIAL OTHER')
            require_gui_source(self.app, str(other)); release.set(); self.settled(d)
        self.assertIsNone(d.discovery); self.assertIsNone(d.plan); self.assertEqual(d.variables,{})
        d.refresh_button.invoke(); self.settled(d)
        self.assertEqual(set(d.variables), {'other.pdf'})
        self.assertIs(d.settings,self.app.settings); self.assertEqual(d.selection(),[])
        self.unchanged()

    def invalidate_preview(self, change):
        d=self.open(); d.all_button.invoke()
        started,release,blocked=self.block('build_remaining_plan')
        with blocked, patch.object(remaining,'write_plan', wraps=remaining.write_plan) as save:
            d.preview_button.invoke(); self.pump(started.is_set)
            change(d); release.set(); self.settled(d)
            self.assertEqual(save.call_count,0)
        self.assertIsNone(d.plan);self.assertEqual(str(d.confirm_button['state']),'disabled')
        self.assertEqual(list((self.sandbox.state/'plans').glob('*.json')),[])
        d.confirm();self.unchanged()

    def test_range_change_cancels_preview_before_save(self):
        self.invalidate_preview(lambda d: self.app.subfolder_var.set(True))
    def test_archive_setting_change_cancels_preview_before_save(self):
        self.invalidate_preview(lambda d: self.app.archive_var.set(True))
    def test_category_setting_change_cancels_preview_before_save(self):
        self.invalidate_preview(lambda d: next(iter(self.app._selection_vars.values())).set(False))
    def test_watch_setting_change_cancels_preview_before_save(self):
        self.invalidate_preview(lambda d: self.app._watch_enabled_var.set(True))
    def test_selection_change_cancels_preview_before_save(self):
        self.invalidate_preview(lambda d: d.variables['b.csv'].set(False))
    def test_source_change_cancels_preview_before_save(self):
        other=self.sandbox.root/'Other';other.mkdir()
        self.invalidate_preview(lambda d: require_gui_source(self.app,str(other)))

    def test_latest_request_replaces_pending_without_task_stack_or_stale_publication(self):
        started,release,blocked=self.block('discover_remaining')
        # One manually gated running task, then replace the single pending slot.
        with blocked as discover:
            self.app.remaining_button.invoke();d=self.app._remaining_dialog;self.pump(started.is_set)
            old=d._task
            for name in ('Other','Latest'):
                source=self.sandbox.root/name;source.mkdir();(source/(name+'.pdf')).write_bytes(b'ARTIFICIAL')
                require_gui_source(self.app,str(source));d.refresh_candidates()
                self.assertIsNotNone(d.preparation.pending)
            latest=d._task
            self.assertIs(d.preparation.active[1],old)
            self.assertIs(d.preparation.pending[1],latest)
            release.set();self.settled(d)
            seen=[call.args[0].downloads_dir for call in discover.call_args_list]
        self.assertEqual(set(d.variables),{'Latest.pdf'})
        self.assertEqual(seen,[str(self.sandbox.downloads),str(self.sandbox.root/'Latest')])
        # Duplicate old completion after the current publication must be ignored.
        before=(dict(d.variables),d.context,d.plan)
        d.preparation.output.put((old,None,'late old failure'))
        d.preparation._poll()
        self.assertEqual((d.variables,d.context,d.plan),before)
        self.assertEqual(self.calls,[]);self.assertEqual(d.selection(),[])
        self.assertTrue(old.cancelled.is_set())
        self.unchanged()

    def test_repeated_clicks_create_one_discovery_and_one_preview(self):
        started,release,blocked=self.block('discover_remaining')
        with blocked as discover:
            self.app.remaining_button.invoke();d=self.app._remaining_dialog;self.pump(started.is_set)
            for _ in range(12): d.refresh_candidates(); self.app.on_remaining_archive()
            self.assertIsNone(d.preparation.pending);self.assertEqual(discover.call_count,1)
            release.set();self.settled(d)
        d.all_button.invoke();started,release,blocked=self.block('build_remaining_plan')
        with blocked as build:
            d.preview_button.invoke();self.pump(started.is_set)
            for _ in range(12): d.preview();d.preview_button.invoke();d.confirm()
            self.assertIsNone(d.preparation.pending);self.assertEqual(build.call_count,1)
            release.set();self.settled(d)
        self.assertIsNotNone(d.plan);self.assertEqual(self.app.session.head,0)
        self.assertEqual(len(list((self.sandbox.state/'plans').glob('*.json'))),1)
        self.unchanged()

    def test_selection_new_preview_replaces_old_and_saves_only_latest(self):
        d=self.open();d.all_button.invoke();started,release,blocked=self.block('build_remaining_plan')
        with blocked:
            d.preview_button.invoke();self.pump(started.is_set);old=d._task
            d.variables['b.csv'].set(False);d.preview_button.invoke()
            self.assertIsNotNone(d.preparation.pending);release.set();self.settled(d)
        self.assertTrue(old.cancelled.is_set());self.assertEqual(d.plan['remaining_selection'],['a.pdf','empty'])
        self.assertEqual(len(list((self.sandbox.state/'plans').glob('*.json'))),1)
        self.assertNotExists(confirmation_path(d.settings,d.plan['plan_id']));self.unchanged()

    def test_close_and_reopen_uses_one_channel_and_drops_old_window(self):
        started,release,blocked=self.block('discover_remaining')
        with blocked:
            self.app.remaining_button.invoke();old=self.app._remaining_dialog;self.pump(started.is_set)
            old.close();self.app.remaining_button.invoke();new=self.app._remaining_dialog
            self.assertIs(old.preparation,new.preparation);self.assertIsNot(old,new)
            release.set();self.settled(new)
        self.assertFalse(old.window.winfo_exists());self.assertIsNone(old.discovery)
        self.assertEqual(set(new.variables),{'a.pdf','b.csv','empty'});self.assertEqual(new.selection(),[])
        self.assertEqual(self.calls,[]);self.unchanged()

    def test_completed_save_cancelled_before_publication_retains_immutable_plan_without_authority(self):
        d=self.open();d.all_button.invoke();started=threading.Event();release=threading.Event();saved=[]
        original=remaining.write_plan
        def save(s,p):
            path=original(s,p);saved.append((path,Path(path).read_bytes(),p['plan_id']))
            started.set();release.wait(5);return path
        with patch.object(remaining,'write_plan',side_effect=save):
            d.preview_button.invoke();self.pump(started.is_set)
            d.close_button.invoke();release.set();self.pump(lambda:d.preparation.active is None)
        path,raw,pid=saved[0];self.assertEqual(Path(path).read_bytes(),raw)
        self.assertIsNone(d.plan);self.assertNotExists(confirmation_path(self.app.settings,pid))
        self.assertIsNone(self.app.plan);self.assertEqual(self.calls,[]);self.unchanged()

    def test_failures_do_not_reuse_prior_authority_and_rediscovery_restores_roundtrip(self):
        d=self.open();old=self.preview(d)
        for symbol in ('build_remaining_plan','write_plan','discover_remaining'):
            with self.subTest(symbol=symbol):
                with patch.object(remaining,symbol,side_effect=OSError('ARTIFICIAL IO FAILURE')):
                    if symbol=='discover_remaining':d.refresh_button.invoke()
                    else:d.preview()
                    self.settled(d)
                self.assertIsNone(d.plan);d.confirm()
                self.assertNotExists(confirmation_path(d.settings,old['plan_id']))
                d.refresh_button.invoke();self.settled(d);self.preview(d)
        plan=d.plan;raw=self.sandbox.plan_file(plan['plan_id']).read_bytes()
        d.confirm_button.invoke();confirm=Path(confirmation_path(d.settings,plan['plan_id']));confirmation=confirm.read_bytes()
        self.app.undo_button.invoke()
        for op in plan['operations']:
            self.assertIdentityMatches(op['source'],op['identity']);self.assertFileContent(op['source'],b'ARTIFICIAL:'+op['rel_path'].encode())
            self.assertNotExists(op['dest'])
        for op in plan['directory_operations']:
            self.assertEqual(list(dir_identity(op['source'])),[op['identity']['st_dev'],op['identity']['st_ino']]);self.assertNotExists(op['dest'])
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(),raw);self.assertEqual(confirm.read_bytes(),confirmation)
        logs={k:Journal(d.settings.state_dir,plan['plan_id'],k).classify().classification for k in ('execution','undo')}
        self.assertEqual(logs,{'execution':'healthy','undo':'healthy'})
        EVIDENCE.append({'test_id':self.id(),'root':str(self.sandbox.root),'plan_id':plan['plan_id'],'identity_bytes_restored':True,'logs':logs,'immutable_records':True})

    def test_worker_never_reads_tk_variables_or_schedules_tk_callbacks(self):
        get=gui.tk.Variable.get;boolean_get=gui.tk.BooleanVar.get;after=gui.tk.Misc.after
        def checked_get(var):
            self.assertEqual(threading.get_ident(),self.main_thread);return get(var)
        def checked_after(widget,*a,**kw):
            self.assertEqual(threading.get_ident(),self.main_thread);return after(widget,*a,**kw)
        def checked_boolean_get(var):
            self.assertEqual(threading.get_ident(),self.main_thread);return boolean_get(var)
        with patch.object(gui.tk.Variable,'get',checked_get),patch.object(gui.tk.BooleanVar,'get',checked_boolean_get),patch.object(gui.tk.Misc,'after',checked_after):
            d=self.open();self.preview(d)
        self.assertIsNotNone(d.plan);self.assertEqual(self.calls,[]);self.unchanged()

    def test_late_worker_failure_after_dialog_close_has_no_popup_or_widget_access(self):
        started,release=threading.Event(),threading.Event()
        def failed(*a,**kw):
            started.set();release.wait(5);raise PermissionError('ARTIFICIAL LATE FAILURE')
        with patch.object(remaining,'discover_remaining',side_effect=failed):
            self.app.remaining_button.invoke();d=self.app._remaining_dialog;self.pump(started.is_set)
            d.close_button.invoke();release.set();self.pump(lambda:d.preparation.active is None)
        self.assertFalse(d.window.winfo_exists());self.assertIsNone(d.plan)
        self.assertEqual(self.calls,[]);self.unchanged()

    def test_old_completion_returning_during_new_task_cannot_displace_current_task(self):
        d=self.open();d.all_button.invoke();started,release,blocked=self.block('build_remaining_plan')
        with blocked:
            d.preview_button.invoke();self.pump(started.is_set);old=d._task
            d.variables['b.csv'].set(False);d.preview_button.invoke();current=d._task
            d.preparation.output.put((current,{'forged':'out-of-order pending completion'},None))
            self.pump(d.preparation.output.empty)
            self.assertIsNone(d.plan);self.assertIs(d.preparation.active[1],old)
            self.assertIs(d.preparation.pending[1],current)
            release.set();self.settled(d)
        self.assertEqual(d.plan['remaining_selection'],['a.pdf','empty'])
        d.preparation.output.put((old,None,'late old failure'))
        d.preparation._poll()
        self.assertEqual(d.plan['remaining_selection'],['a.pdf','empty'])
        self.assertEqual(self.calls,[]);self.unchanged()
