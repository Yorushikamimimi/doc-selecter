"""Headless state and safety contract tests for the webpage controller."""
import json
import threading
from pathlib import Path
from unittest.mock import patch

from doc_selecter import ui_controller as ui
from doc_selecter.atomic_move import lstat_identity
from doc_selecter.executor import ExecutionRefused
from doc_selecter.locking import write_plan_state
from doc_selecter.planner import confirmation_path
from tests.harness import SandboxTestCase


class ControllerTests(SandboxTestCase):
    def controller(self):
        c = ui.Controller(self.sandbox.settings)
        def cleanup():
            c.wait_idle(); c.close()
        self.addCleanup(cleanup)
        return c

    def preview(self, names=('人工.pdf', '图片.png', '未知.example')):
        for name in names:
            self.sandbox.add_file(name, b'ARTIFICIAL-' + name.encode())
        c = self.controller(); c.select_source(str(self.sandbox.downloads)); c.wait_idle()
        self.assertEqual(c.phase, 'preview', c.error)
        return c

    def state_bytes(self):
        return {str(p.relative_to(self.sandbox.state)): p.read_bytes()
                for p in self.sandbox.state.rglob('*') if p.is_file()}

    def organize(self, c):
        c.organize(c.get_state()['plan_hash'], True); v = c.wait_idle()
        self.assertEqual(v['phase'], 'done', v['error'])
        return v

    def test_empty_source_preview_filter_category_and_roundtrip(self):
        c = self.controller()
        self.assertEqual(c.get_state()['phase'], 'empty')
        self.assertFalse(c.get_state()['can_organize'])
        before = {}
        for name in ('人工.pdf', '图片.png', '未知.example'):
            p = self.sandbox.add_file(name, name.encode())
            before[str(p)] = (lstat_identity(str(p)), p.read_bytes())
        c.select_source(str(self.sandbox.downloads)); v = c.wait_idle()
        self.assertEqual(v['phase'], 'preview'); self.assertIsNone(c.confirmed_hash)
        plan = c.plan; state = self.state_bytes()
        v = c.set_filter('文档资料')
        self.assertEqual([r['name'] for r in v['rows']], ['人工.pdf'])
        self.assertIs(c.plan, plan); self.assertEqual(self.state_bytes(), state)
        c.set_categories([k for k in c.categories if k != '待确认']); c.wait_idle()
        self.assertNotEqual(c.plan['plan_hash'], plan['plan_hash'])
        self.assertTrue(all(Path(p).is_file() for p in before))
        self.assertIsNone(c.confirmed_hash)
        v = self.organize(c); self.assertFalse(Path(str(self.sandbox.downloads / '人工.pdf')).exists())
        self.assertTrue((self.sandbox.downloads / '未知.example').is_file())
        c.undo(v['undo_token'], True); v = c.wait_idle()
        self.assertEqual(v['phase'], 'undone', v['error'])
        self.assertTrue(all((lstat_identity(p), Path(p).read_bytes()) == old for p, old in before.items()))
        self.assertFalse(v['can_organize']); self.assertFalse(v['can_undo'])

    def test_no_confirmation_or_wrong_hash_never_moves_or_writes(self):
        c = self.preview(); before = self.state_bytes()
        for plan_hash, confirmed in ((c.plan['plan_hash'], False), ('stale', True), (None, True)):
            with self.subTest(plan_hash=plan_hash, confirmed=confirmed):
                with self.assertRaises(ui.RequestRefused): c.organize(plan_hash, confirmed)
                self.assertEqual(self.state_bytes(), before)
                self.assertTrue(all(Path(op['source']).is_file() for op in c.plan['operations']))

    def test_stale_confirm_after_category_change_is_refused(self):
        c = self.preview(); old = c.plan
        c.set_categories(['文档资料']); c.wait_idle(); before = self.state_bytes()
        with self.assertRaises(ui.RequestRefused): c.organize(old['plan_hash'], True)
        self.assertEqual(self.state_bytes(), before)
        self.assertFalse(Path(confirmation_path(c.settings, old['plan_id'])).exists())

    def test_archive_setting_invalidates_confirmation(self):
        c = self.preview(); old_hash = c.plan['plan_hash']
        c.settings_set(True, False)
        with self.assertRaises(ui.RequestRefused): c.organize(old_hash, True)
        self.assertTrue(all(Path(op['source']).is_file() for op in c.plan['operations']))

    def test_external_saved_plan_change_is_refused_before_confirmation(self):
        c = self.preview(); plan_file = self.sandbox.plan_file(c.plan['plan_id'])
        modified = json.loads(plan_file.read_text()); modified['category_selection'] = []
        plan_file.write_text(json.dumps(modified))
        c.organize(c.plan['plan_hash'], True); c.wait_idle()
        self.assertIn(c.phase, ('error', 'needs_review'))
        self.assertFalse(Path(confirmation_path(c.settings, c.plan['plan_id'])).exists())
        self.assertTrue(all(Path(op['source']).is_file() for op in c.plan['operations']))

    def test_collision_is_real_skip_and_target_is_not_overwritten(self):
        target = self.sandbox.target_dir('安装包') / '人工.dmg'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'ARTIFICIAL CONFLICT')
        c = self.preview(('人工.dmg', '人工.pdf'))
        self.assertEqual(c.get_state()['skipped_count'], 1)
        v = self.organize(c)
        self.assertEqual([r['state'] for r in v['rows']].count('sk'), 1)
        self.assertEqual(target.read_bytes(), b'ARTIFICIAL CONFLICT')
        c.undo(v['undo_token'], True); c.wait_idle()
        self.assertEqual(c.phase, 'undone'); self.assertEqual(target.read_bytes(), b'ARTIFICIAL CONFLICT')
        self.assertTrue((self.sandbox.downloads / '人工.dmg').is_file())

    def test_consumed_history_cannot_execute_twice(self):
        c = self.preview(); old = c.plan['plan_hash']; self.organize(c)
        before = self.state_bytes()
        with self.assertRaises(ui.RequestRefused): c.organize(old, True)
        self.assertEqual(self.state_bytes(), before)

    def test_reopen_skips_latest_preview_but_preserves_older_executed_history(self):
        c = self.preview(); self.organize(c); old = c.plan
        self.sandbox.add_file('新增.pdf'); c.preview(); c.wait_idle()
        reopened = self.controller(); reopened.select_source(str(self.sandbox.downloads)); v = reopened.wait_idle()
        self.assertEqual(reopened.plan['plan_id'], old['plan_id'])
        self.assertFalse(v['can_organize']); self.assertTrue(v['can_undo'])
        self.assertIsNone(reopened.confirmed_hash)

    def test_busy_rejects_reentry_and_closing_during_move(self):
        c = self.preview(); entered, release = threading.Event(), threading.Event()
        original = ui.execute_plan
        def blocked(*args, **kwargs):
            entered.set(); release.wait(5); return original(*args, **kwargs)
        with patch.object(ui, 'execute_plan', side_effect=blocked):
            c.organize(c.plan['plan_hash'], True); self.assertTrue(entered.wait(2))
            try:
                self.assertTrue(c.get_state()['busy']); self.assertFalse(c.close())
                with self.assertRaises(ui.RequestRefused): c.organize(c.plan['plan_hash'], True)
                with self.assertRaises(ui.RequestRefused): c.set_categories([])
                with self.assertRaises(ui.RequestRefused): c.select_source(str(self.sandbox.downloads))
            finally:
                release.set(); c.wait_idle()
        self.assertEqual(c.phase, 'done')

    def test_executor_refusal_is_visible_and_never_claims_success(self):
        c = self.preview()
        with patch.object(ui, 'execute_plan', side_effect=ExecutionRefused('ARTIFICIAL STOP')):
            c.organize(c.plan['plan_hash'], True); v = c.wait_idle()
        self.assertEqual(v['banner']['kind'], 'warn')
        self.assertFalse(any(r['state'] == 'ok' for r in v['rows']))
        self.assertTrue(all(Path(op['source']).is_file() for op in c.plan['operations']))

    def test_partial_failure_preserves_real_moved_rows_and_warns(self):
        c = self.preview(('人工.pdf', '图片.png')); original = ui.execute_plan
        def failed(settings, plan_id, plan_hash, **kwargs):
            def hook(event, op):
                if event == 'before_move' and op['op_id'] == 'op-0002':
                    Path(op['source']).write_bytes(b'ARTIFICIAL CHANGED')
            return original(settings, plan_id, plan_hash, hook=hook, kind='gui')
        with patch.object(ui, 'execute_plan', side_effect=failed):
            c.organize(c.plan['plan_hash'], True); v = c.wait_idle()
        self.assertEqual(v['banner']['kind'], 'warn')
        self.assertEqual(sum(r['state'] == 'ok' for r in v['rows']), 1)
        self.assertFalse(v['can_organize'])

    def test_undo_occupied_source_is_not_overwritten_and_reports_blocked(self):
        c = self.preview(('人工.pdf',)); v = self.organize(c)
        source = self.sandbox.downloads / '人工.pdf'; source.write_bytes(b'ARTIFICIAL OCCUPANT')
        c.undo(v['undo_token'], True); v = c.wait_idle()
        self.assertEqual(source.read_bytes(), b'ARTIFICIAL OCCUPANT')
        self.assertEqual(v['phase'], 'needs_review'); self.assertEqual(v['banner']['kind'], 'warn')
        self.assertFalse(any(r['state'] == 'back' for r in v['rows']))

    def test_recovery_requires_current_credential_and_preserves_safety(self):
        c = self.preview(('人工.pdf',)); self.organize(c)
        write_plan_state(c.settings, c.plan['plan_id'], phase='needs_review')
        c._refresh_history()
        self.assertFalse(c.get_state()['can_undo'])
        with self.assertRaises(ui.RequestRefused): c.recover_check('stale')
        c.recover_check(c.get_history()['current_token']); c.wait_idle()
        self.assertFalse(c.get_state()['can_organize'])
        self.assertTrue(Path(c.plan['operations'][0]['dest']).is_file())

    def test_remaining_requires_selection_preview_and_bound_confirmation(self):
        c = self.preview(('人工.pdf',))
        c.remaining_discover(); c.wait_idle()
        self.assertEqual(c.remaining['selected'], [])
        c.remaining_select(['人工.pdf']); c.remaining_preview(); c.wait_idle()
        plan_hash = c.remaining['plan_hash']; self.assertTrue(plan_hash)
        with self.assertRaises(ui.RequestRefused): c.remaining_organize('stale', True)
        self.assertTrue((self.sandbox.downloads / '人工.pdf').is_file())
        c.remaining_organize(plan_hash, True); v = c.wait_idle()
        self.assertEqual(v['phase'], 'done', v['error'])
        c.undo(v['undo_token'], True); c.wait_idle()
        self.assertEqual(c.phase, 'undone'); self.assertTrue((self.sandbox.downloads / '人工.pdf').is_file())

    def test_remaining_candidate_identity_change_revokes_preview(self):
        c = self.preview(('人工.pdf',)); c.remaining_discover(); c.wait_idle()
        c.remaining_select(['人工.pdf']); (self.sandbox.downloads / '人工.pdf').write_bytes(b'ARTIFICIAL REPLACED')
        c.remaining_preview(); c.wait_idle()
        self.assertIsNone(c._remaining_plan)
        self.assertEqual((self.sandbox.downloads / '人工.pdf').read_bytes(), b'ARTIFICIAL REPLACED')

    def test_remaining_cancel_discards_late_discovery(self):
        c = self.preview(('人工.pdf',)); entered, release = threading.Event(), threading.Event()
        original = ui.discover_remaining
        def slow(*a, **kw):
            entered.set(); release.wait(5); return original(*a, **kw)
        with patch.object(ui, 'discover_remaining', side_effect=slow):
            c.remaining_discover(); self.assertTrue(entered.wait(2))
            c.remaining_cancel(); release.set(); c.wait_idle()
        self.assertIsNone(c.remaining); self.assertIsNone(c._remaining_discovery)

    def test_finder_rejects_symlink_and_missing_target(self):
        c = self.preview(('人工.pdf',))
        with self.assertRaises(ui.RequestRefused): c.target_to_open()
        self.organize(c); target = Path(c.settings.target_root)
        self.assertEqual(c.target_to_open(), str(target))
        moved = target.with_name('artificial-target'); target.rename(moved); target.symlink_to(moved)
        with self.assertRaises(ui.RequestRefused): c.target_to_open()

    def test_subfolder_choice_does_not_expand_to_unselected_siblings(self):
        c = self.preview(('Chosen/a.pdf', 'Other/b.pdf'))
        c.list_folders(); c.wait_idle()
        c.set_subfolders(True, ['Chosen']); c.wait_idle()
        self.assertEqual(c.plan['selected_scope'], ['', 'Chosen'])
        self.assertEqual([op['rel_path'] for op in c.plan['operations']], ['Chosen/a.pdf'])
        self.organize(c)
        self.assertTrue((self.sandbox.downloads / 'Other/b.pdf').is_file())

    def test_view_is_json_safe_and_caller_cannot_mutate_controller(self):
        c = self.preview(); v = c.get_state(); json.dumps(v, ensure_ascii=False)
        v['categories'].clear(); v['rows'].clear(); v['subfolders']['selected'].append('other')
        self.assertTrue(c.get_state()['categories']); self.assertTrue(c.get_state()['rows'])
        self.assertEqual(c.scope, [])

    def test_manual_new_file_checks_keep_settling_observations_without_moving(self):
        c = self.preview(('人工.pdf',)); now = [0.0]
        c.watcher.set_clock(lambda: now[0]); before = self.state_bytes()
        source = self.sandbox.add_file('新文件.png', b'ARTIFICIAL')
        c.check_new_files(); c.wait_idle()
        self.assertEqual(c.discovery['new_files'], [])
        now[0] = 10
        c.check_new_files(); v = c.wait_idle()
        self.assertEqual(c.discovery['new_files'], ['新文件.png'])
        self.assertEqual(v['banner']['kind'], 'info')
        self.assertEqual(source.read_bytes(), b'ARTIFICIAL')
        self.assertEqual(self.state_bytes(), before)
        self.assertFalse(c.watcher.enabled)

    def test_return_to_credential_is_revoked_after_new_preview(self):
        c = self.preview(('人工.pdf',)); self.organize(c)
        h = c.get_history(); c.preview(); c.wait_idle()
        with self.assertRaises(ui.RequestRefused): c.return_to(0,h['root_token'],True)
        self.assertTrue((self.sandbox.target_dir('文档资料')/'PDF/人工.pdf').is_file())

    def test_history_has_full_read_only_rows_for_each_version(self):
        c = self.preview(('人工.pdf',)); self.organize(c)
        before = self.state_bytes()
        h = c.get_history(1)
        self.assertEqual(h['rows'][0]['name'],'人工.pdf')
        self.assertEqual(h['rows'][0]['state'],'ok')
        self.assertEqual(before,self.state_bytes())
        with self.assertRaises(ui.RequestRefused): c.get_history(100)
