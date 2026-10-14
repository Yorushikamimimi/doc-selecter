"""Explicit v4 permission, real atomic moves, original-object undo and old history.

Every fixture is artificial. Fault hooks represent external changes; those cases
must not be reported as a fully restored roundtrip.
"""
from __future__ import annotations
import copy
import errno
import json
import os
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path
from unittest.mock import patch

from tests.harness import SandboxTestCase
from doc_selecter.remaining_archive import build_remaining_plan, discover_remaining
from doc_selecter.planner import (PlanError, build_plan, write_plan, write_confirmation,
    confirmation_path, load_verified_plan)
from doc_selecter.executor import execute_plan, recover_execution, ExecutionRefused
from doc_selecter.undo import undo_plan, UndoRefused
from doc_selecter.atomic_move import AtomicMover, MoveFailed, lstat_identity, dir_identity
from doc_selecter.journal import Journal, JournalWriteFailed
from doc_selecter.locking import GlobalLock, LockBusy, read_plan_state
from doc_selecter.plan_hash import compute_plan_hash
from doc_selecter.gui_session import OperationSession
from doc_selecter.gui_source import selected_settings
from doc_selecter.snapshot import read_snapshot
from doc_selecter.watcher import NewFileWatcher

EVIDENCE = []


class SimulatedExit(BaseException):
    pass


class RemainingArchiveTests(SandboxTestCase):
    def plan(self, selection, *, scope=(), settings=None, confirm=True, plan_id=None):
        settings = settings or self.sandbox.settings
        p = build_remaining_plan(settings, selection=selection, scope_dirs=list(scope),
                                 recursive=bool(scope), plan_id=plan_id).plan
        write_plan(settings, p)
        if confirm:
            write_confirmation(settings, p['plan_id'], p['plan_hash'])
        return p

    def execute(self, p, **kw):
        return execute_plan(self.sandbox.settings, p['plan_id'], p['plan_hash'], **kw)

    def frozen(self, p, settings=None):
        settings = settings or self.sandbox.settings
        return {str(path): path.read_bytes() for path in
                (Path(settings.state_dir)/'plans'/f"{p['plan_id']}.json",
                 Path(confirmation_path(settings, p['plan_id'])))}

    def roundtrip(self, p, *, settings=None, recover=True):
        settings = settings or self.sandbox.settings
        immutable = self.frozen(p, settings)
        if recover:
            report = recover_execution(settings, p['plan_id'])
            self.assertEqual(report['unknown'], [], report)
        r = undo_plan(settings, p['plan_id'], confirm=True)
        self.assertEqual(r.undo_status, 'completed', r)
        for op in p['operations']:
            self.assertIdentityMatches(op['source'], op['identity'])
            self.assertFileContent(op['source'], b'ARTIFICIAL:' + op['rel_path'].encode())
            self.assertNotExists(op['dest'])
        for op in p.get('directory_operations', []):
            self.assertEqual(list(dir_identity(op['source'])),
                             [op['identity']['st_dev'], op['identity']['st_ino']])
            self.assertTrue(stat.S_ISDIR(os.lstat(op['source']).st_mode))
            self.assertEqual(os.listdir(op['source']), [])
            self.assertNotExists(op['dest'])
        self.assertEqual(self.frozen(p, settings), immutable)
        self.assertEqual(read_plan_state(settings, p['plan_id'])['phase'], 'undone')
        self.assertEqual(read_snapshot(settings, p['plan_id'])['undo_status'], 'completed')
        logs = {k: Journal(settings.state_dir, p['plan_id'], k).classify().classification
                for k in ('execution', 'undo')}
        self.assertEqual(logs, {'execution': 'healthy', 'undo': 'healthy'})
        EVIDENCE.append(dict(route=self.id(), root=str(self.sandbox.root), plan_id=p['plan_id'],
                             files=len(p['operations']), directories=len(p.get('directory_operations', [])),
                             identity_bytes_and_immutable_records=True, logs=logs))
        return r

    def file(self, name):
        return self.sandbox.add_file(name, b'ARTIFICIAL:' + unicodedata.normalize('NFC', name).encode())

    def test_default_unselected_cannot_confirm_or_create_layout(self):
        self.file('report.pdf'); self.sandbox.add_dir('empty')
        p = self.plan([], confirm=False)
        self.assertEqual(p['remaining_selection'], [])
        with self.assertRaises(PlanError):
            write_confirmation(self.sandbox.settings, p['plan_id'], p['plan_hash'])
        with self.assertRaises((PlanError, ExecutionRefused)):
            self.execute(p)
        self.assertNotExists(confirmation_path(self.sandbox.settings, p['plan_id']))
        self.assertNotExists(self.sandbox.downloads/'下载分类')

    def test_original_empty_directory_only_roundtrip_and_session(self):
        self.sandbox.add_dir('empty')
        p = self.plan(['empty']); r = self.execute(p)
        self.assertEqual((r.moved, r.archived_dirs, r.stopped), (0, 1, False))
        session = OperationSession(); s = session.track(self.sandbox.settings, p['plan_id'])
        self.assertEqual((s.moved, s.archived_dirs), (0, 1))
        session.checked_batch(s.settings, s.plan_id); self.assertTrue(s.checked)
        result = session.return_to(0, undo_plan)
        self.assertTrue(result.reached, result)
        self.assertEqual((result.restored, result.restored_dirs), (0, 1))
        self.roundtrip(p)

    def test_ordinary_files_keep_relative_locations_and_literal_backslash(self):
        names = ['说明.pdf', 'report' + chr(92) + '2026.pdf', '组/深层/report.pdf']
        for name in names: self.file(name)
        p = self.plan(names, scope=['组/深层'])
        self.assertTrue(all('/剩余文件/' + op['rel_path'] in op['dest'] for op in p['operations']))
        self.assertEqual(self.execute(p).moved, 3)
        for op in p['operations']: self.assertIdentityMatches(op['dest'], op['identity'])
        self.roundtrip(p)

    def test_mixed_files_empty_directories_and_nfd(self):
        raw = 'Cafe' + chr(0x0301)
        self.file(raw + '/deep/说明.pdf'); self.file('normal.csv')
        self.sandbox.add_dir(raw + '/empty')
        selected = [unicodedata.normalize('NFC', raw) + '/deep/说明.pdf',
                    unicodedata.normalize('NFC', raw) + '/empty', 'normal.csv']
        p = self.plan(selected, scope=[raw]); r = self.execute(p)
        self.assertEqual((r.moved, r.archived_dirs), (2, 1))
        self.roundtrip(p)
        self.assertIn('说明.pdf', os.listdir(self.sandbox.downloads/raw/'deep'))

    def test_selected_deep_scope_does_not_grant_siblings_or_transit_files(self):
        self.file('top.pdf'); self.file('parent/transit.pdf'); self.file('parent/chosen/a.pdf')
        self.file('parent/other/b.pdf'); self.sandbox.add_dir('parent/chosen/empty')
        d = discover_remaining(self.sandbox.settings, scope_dirs=['parent/chosen'], recursive=True)
        self.assertEqual({c['rel_path'] for c in d.candidates},
                         {'top.pdf', 'parent/chosen/a.pdf', 'parent/chosen/empty'})
        with self.assertRaises(PlanError):
            self.plan(['parent/other/b.pdf'], scope=['parent/chosen'])
        p = self.plan(['parent/chosen/empty'], scope=['parent/chosen'])
        self.assertEqual(self.execute(p).archived_dirs, 1); self.roundtrip(p)
        self.assertExists(self.sandbox.downloads/'parent/other/b.pdf')

    def test_nonempty_unselected_directory_cannot_be_archived(self):
        self.file('unselected/keep.pdf')
        d = discover_remaining(self.sandbox.settings)
        self.assertEqual(d.candidates, [])
        self.assertEqual(d.excluded_counts['未选择的非空文件夹'], 1)
        with self.assertRaises(PlanError): self.plan(['unselected'])
        with self.assertRaises(PlanError): self.plan(['unselected/keep.pdf'])

    def test_sensitive_names_filtered_before_stat_never_persisted(self):
        self.file('合同.pdf'); self.sandbox.add_dir('private-items'); self.file('safe.pdf')
        observed = []
        original_stat = os.stat
        def traced(path, *a, **kw):
            observed.append(str(path)); return original_stat(path, *a, **kw)
        with patch('os.stat', side_effect=traced):
            d = discover_remaining(self.sandbox.settings)
        self.assertFalse(any('合同' in v or 'private-items' in v for v in observed), observed)
        self.assertEqual([c['rel_path'] for c in d.candidates], ['safe.pdf'])
        self.assertEqual(d.excluded_counts['疑似私人项目'], 2)
        p = self.plan(['safe.pdf'])
        text = self.sandbox.plan_file(p['plan_id']).read_text()
        self.assertNotIn('合同', text); self.assertNotIn('private-items', text)
        self.assertEqual(self.execute(p).moved, 1); self.roundtrip(p)

    def test_hidden_package_temporary_project_link_and_target_excluded(self):
        for name in ('.hidden.pdf', 'unfinished.crdownload', 'app.app/keep.pdf', 'project/package.json',
                     'project/a.pdf', '下载分类/已归档/old/keep.pdf'): self.file(name)
        self.sandbox.add_dir('.hidden-dir'); self.sandbox.add_symlink('link.pdf', 'project/a.pdf')
        self.sandbox.add_dir('ordinary'); self.file('okay.pdf')
        d = discover_remaining(self.sandbox.settings)
        self.assertEqual({c['rel_path'] for c in d.candidates}, {'ordinary', 'okay.pdf'})
        p = self.plan(['ordinary', 'okay.pdf'])
        self.assertEqual(self.execute(p).archived_dirs, 1)
        again = discover_remaining(self.sandbox.settings)
        self.assertEqual(again.candidates, [])
        from doc_selecter.scanner import scan_top_level
        self.assertFalse(any('已归档' in e.rel_path for e in scan_top_level(self.sandbox.settings.downloads_dir, target_parent_name=self.sandbox.settings.target_parent_name).included))
        watcher = NewFileWatcher(); watcher.enable()
        self.assertEqual(watcher.check_once(downloads_dir=str(self.sandbox.downloads), target_parent_name='下载分类').new_files, [])
        self.roundtrip(p)

    def test_target_area_cannot_be_an_explicit_scope(self):
        self.file('下载分类/report.pdf')
        with self.assertRaises(PlanError):
            discover_remaining(self.sandbox.settings, scope_dirs=['下载分类'], recursive=True)

    def test_candidate_selection_is_fresh_not_previous_exclusion_authority(self):
        self.file('a.pdf')
        d = discover_remaining(self.sandbox.settings)
        self.file('b.pdf')
        with self.assertRaisesRegex(PlanError, '变化'):
            build_remaining_plan(self.sandbox.settings, selection=['a.pdf'], expected_candidates=d.candidates)

    def test_file_identity_change_after_preview_refuses_confirmation_and_execution(self):
        self.file('a.pdf'); p = self.plan(['a.pdf'], confirm=False)
        self.file('a.pdf').write_bytes(b'CHANGED')
        with self.assertRaises(PlanError): write_confirmation(self.sandbox.settings, p['plan_id'], p['plan_hash'])
        with self.assertRaises((PlanError, ExecutionRefused)): self.execute(p)
        self.assertNotExists(self.sandbox.downloads/'下载分类')

    def test_directory_becomes_nonempty_before_confirmation_is_refused(self):
        self.sandbox.add_dir('empty'); p = self.plan(['empty'], confirm=False)
        self.file('empty/new.pdf')
        with self.assertRaises(PlanError): write_confirmation(self.sandbox.settings, p['plan_id'], p['plan_hash'])
        self.assertExists(self.sandbox.downloads/'empty/new.pdf')

    def test_symlink_replacement_never_follows_target_metadata(self):
        self.file('a.pdf'); p = self.plan(['a.pdf'], confirm=False)
        sensitive = self.file('合同.pdf')
        os.unlink(self.sandbox.downloads/'a.pdf'); os.symlink(sensitive, self.sandbox.downloads/'a.pdf')
        observed = []; original_stat = os.stat
        def traced(path, *args, **kw):
            observed.append(str(path)); return original_stat(path, *args, **kw)
        with patch('os.stat', side_effect=traced):
            with self.assertRaises(PlanError): write_confirmation(self.sandbox.settings, p['plan_id'], p['plan_hash'])
            with self.assertRaises((PlanError, ExecutionRefused)): self.execute(p)
        self.assertFalse(any('合同' in v for v in observed), observed)
        self.assertNotExists(self.sandbox.downloads/'下载分类')

    def test_parent_replaced_or_linked_refuses_zero_moves(self):
        self.file('group/a.pdf'); p = self.plan(['group/a.pdf'], scope=['group'])
        os.rename(self.sandbox.downloads/'group', self.sandbox.downloads/'held')
        os.symlink('held', self.sandbox.downloads/'group')
        with self.assertRaises((PlanError, ExecutionRefused, OSError)): self.execute(p)
        self.assertExists(self.sandbox.downloads/'held/a.pdf')

    def test_new_project_marker_blocks_remaining_archive(self):
        self.file('group/a.pdf'); p = self.plan(['group/a.pdf'], scope=['group'])
        self.file('group/package.json')
        with self.assertRaises((PlanError, ExecutionRefused)): self.execute(p)
        self.assertExists(self.sandbox.downloads/'group/a.pdf')

    def test_permission_denied_is_not_empty_or_missing(self):
        directory = self.sandbox.add_dir('empty')
        original_list = os.listdir
        def denied(value):
            if isinstance(value, int) and os.fstat(value).st_ino == directory.stat().st_ino:
                raise PermissionError(errno.EACCES, 'artificial denial')
            return original_list(value)
        with patch('os.listdir', side_effect=denied):
            d = discover_remaining(self.sandbox.settings)
            self.assertEqual(d.candidates, []); self.assertTrue(d.issues)
            with self.assertRaises(PlanError): self.plan(['empty'])

    def test_scanning_changes_refuse_not_partial_authorization(self):
        self.file('a.pdf'); calls = 0; original = os.listdir
        root_inode = self.sandbox.downloads.stat().st_ino
        def changed(fd):
            nonlocal calls
            if isinstance(fd, int) and os.fstat(fd).st_ino == root_inode:
                calls += 1
                if calls == 2: self.file('b.pdf')
            return original(fd)
        with patch('os.listdir', side_effect=changed):
            d = discover_remaining(self.sandbox.settings)
        self.assertTrue(d.issues, d)

    def test_file_target_collision_is_local_never_overwrites(self):
        self.file('a.pdf'); self.file('b.pdf')
        target = self.sandbox.downloads/'下载分类/已归档/plan-test/剩余文件/a.pdf'
        target.parent.mkdir(parents=True); target.write_bytes(b'EXTERNAL')
        external = lstat_identity(str(target)).as_dict()
        p = self.plan(['a.pdf', 'b.pdf'], plan_id='plan-test'); r = self.execute(p)
        self.assertEqual((r.moved, r.skipped, r.stopped), (1, 1, False), r)
        self.assertFileContent(target, b'EXTERNAL'); self.assertIdentityMatches(target, external)
        u = undo_plan(self.sandbox.settings, p['plan_id'], confirm=True)
        self.assertEqual((u.restored, u.undo_status), (1, 'completed'))
        self.assertFileContent(self.sandbox.downloads/'a.pdf', b'ARTIFICIAL:a.pdf')

    def test_directory_target_collision_is_local_original_object_kept(self):
        self.sandbox.add_dir('empty'); self.sandbox.add_dir('second')
        target = self.sandbox.downloads/'下载分类/已归档/plan-test/空文件夹/dir-0001/empty'
        target.mkdir(parents=True); external = list(dir_identity(str(target)))
        p = self.plan(['empty', 'second'], plan_id='plan-test'); r = self.execute(p)
        self.assertEqual((r.archived_dirs, r.dirs_skipped, r.stopped), (1, 1, False), r)
        self.assertEqual(list(dir_identity(str(target))), external)
        u = undo_plan(self.sandbox.settings, p['plan_id'], confirm=True)
        self.assertEqual((u.restored_dirs, u.undo_status), (1, 'completed'))

    def test_directory_exdev_is_local_and_never_counts_success(self):
        self.sandbox.add_dir('empty'); p = self.plan(['empty'])
        original = AtomicMover.rename_excl_at
        def exdev(mover, sfd, src, dfd, dst):
            if src == 'empty': raise MoveFailed(errno.EXDEV, 'artificial cross volume')
            return original(mover, sfd, src, dfd, dst)
        with patch.object(AtomicMover, 'rename_excl_at', new=exdev): r = self.execute(p)
        self.assertEqual((r.archived_dirs, r.dirs_skipped, r.stopped), (0, 1, False))
        self.assertExists(p['directory_operations'][0]['source'])

    def test_directory_io_failure_is_global_no_false_success(self):
        self.sandbox.add_dir('empty'); self.sandbox.add_dir('second'); p = self.plan(['empty', 'second'])
        original = AtomicMover.rename_excl_at
        def denied(mover, sfd, src, dfd, dst):
            if src == 'empty': raise PermissionError(errno.EACCES, 'artificial failure')
            return original(mover, sfd, src, dfd, dst)
        with patch.object(AtomicMover, 'rename_excl_at', new=denied): r = self.execute(p)
        self.assertTrue(r.stopped); self.assertEqual((r.archived_dirs, r.dirs_failed, r.dirs_not_attempted), (0, 1, 1))

    def test_content_added_after_empty_check_moves_but_requires_review(self):
        self.sandbox.add_dir('empty'); p = self.plan(['empty'])
        def hook(event, op):
            if event == 'before_directory_move': Path(op['source'], 'added.pdf').write_bytes(b'EXTERNAL')
        r = self.execute(p, hook=hook)
        self.assertTrue(r.stopped); self.assertEqual((r.archived_dirs, r.dirs_failed), (0, 1))
        op = p['directory_operations'][0]
        self.assertFileContent(Path(op['dest'])/'added.pdf', b'EXTERNAL')
        before = self.sandbox.journal(p['plan_id']).generation_file(1)
        evidence = Path(before).read_bytes()
        with self.assertRaises((PlanError, ExecutionRefused)): recover_execution(self.sandbox.settings, p['plan_id'])
        with self.assertRaises((PlanError, UndoRefused)): undo_plan(self.sandbox.settings, p['plan_id'], confirm=True)
        self.assertEqual(Path(before).read_bytes(), evidence)
        self.assertNotExists(op['source'])  # No blind automatic reverse.

    def test_directory_identity_changed_after_move_is_unknown_not_success(self):
        self.sandbox.add_dir('empty'); p = self.plan(['empty'])
        def hook(event, op):
            if event == 'after_directory_move':
                os.rename(op['dest'], op['dest'] + '-held'); os.mkdir(op['dest'])
        r = self.execute(p, hook=hook)
        self.assertTrue(r.stopped); self.assertEqual(r.archived_dirs, 0)
        with self.assertRaises((PlanError, ExecutionRefused)): recover_execution(self.sandbox.settings, p['plan_id'])

    def test_directory_intent_after_move_recovery_and_undo_idempotent(self):
        self.sandbox.add_dir('empty'); p = self.plan(['empty'])
        def hook(event, op):
            if event == 'before_directory_result': raise SimulatedExit()
        with self.assertRaises(SimulatedExit): self.execute(p, hook=hook)
        session = OperationSession(); s = session.track(self.sandbox.settings, p['plan_id'])
        self.assertEqual(s.uncertain_dirs, 1)
        report = recover_execution(self.sandbox.settings, p['plan_id'])
        self.assertEqual(report['recovered'], ['dir-0001']); self.assertEqual(report['unknown'], [])
        session.checked_batch(s.settings, s.plan_id); self.assertTrue(s.checked)
        log = Path(self.sandbox.journal(p['plan_id']).generation_file(1)).read_bytes()
        self.assertEqual(recover_execution(s.settings, s.plan_id)['recovered'], [])
        self.assertEqual(Path(self.sandbox.journal(p['plan_id']).generation_file(1)).read_bytes(), log)
        self.roundtrip(p)

    def test_directory_intent_before_move_recovers_known_no_move(self):
        self.sandbox.add_dir('empty'); p = self.plan(['empty'])
        def hook(event, op):
            if event == 'before_directory_move': raise SimulatedExit()
        with self.assertRaises(SimulatedExit): self.execute(p, hook=hook)
        report = recover_execution(self.sandbox.settings, p['plan_id'])
        self.assertEqual(report['unknown'], []); self.assertEqual(report['recovered'], [])
        u = undo_plan(self.sandbox.settings, p['plan_id'], confirm=True)
        self.assertEqual(u.restored_dirs, 0); self.assertExists(p['directory_operations'][0]['source'])

    def test_file_intent_result_write_failure_recover_original_identity(self):
        self.file('a.pdf'); p = self.plan(['a.pdf'])
        append = Journal.append
        def fail(journal, event, **kw):
            if event.get('event') == 'result' and event.get('op_id') == 'op-0001':
                raise JournalWriteFailed('artificial result failure')
            return append(journal, event, **kw)
        with patch.object(Journal, 'append', new=fail): r = self.execute(p)
        self.assertTrue(r.stopped)
        report = recover_execution(self.sandbox.settings, p['plan_id'])
        self.assertEqual(report['recovered'], ['op-0001']); self.roundtrip(p)

    def test_directory_undo_result_interruption_recovers_without_replacing_object(self):
        self.sandbox.add_dir('empty'); p = self.plan(['empty']); self.execute(p)
        def hook(event, op):
            if event == 'after_directory_restore': raise SimulatedExit()
        with self.assertRaises(SimulatedExit): undo_plan(self.sandbox.settings, p['plan_id'], confirm=True, hook=hook)
        report = recover_execution(self.sandbox.settings, p['plan_id'])
        self.assertEqual(report['recovered'], ['dir-0001']); self.roundtrip(p)

    def test_corrupted_log_refuses_recovery_undo_without_repair(self):
        self.file('a.pdf'); self.sandbox.add_dir('empty'); p = self.plan(['a.pdf', 'empty']); self.execute(p)
        path = Path(self.sandbox.journal(p['plan_id']).generation_file(1))
        # Interior/leading corruption, not the legacy repairable trailing fragment.
        path.write_bytes(b'NOT JSON\n' + path.read_bytes())
        evidence = path.read_bytes(); immutable = self.frozen(p)
        report = recover_execution(self.sandbox.settings, p['plan_id'])
        self.assertEqual(report['phase'], 'corrupted'); self.assertEqual(report['recovered'], [])
        with self.assertRaises((PlanError, UndoRefused)): undo_plan(self.sandbox.settings, p['plan_id'], confirm=True)
        self.assertEqual(path.read_bytes(), evidence); self.assertEqual(self.frozen(p), immutable)
        self.assertNotExists(p['operations'][0]['source'])

    def test_lock_busy_refuses_before_capability_or_movement(self):
        self.file('a.pdf'); p = self.plan(['a.pdf'])
        lock = GlobalLock(self.sandbox.settings); handle = lock.acquire(kind='test', action='held')
        try:
            with patch.object(AtomicMover, 'probe_directory_capability', side_effect=AssertionError('must not probe')):
                with self.assertRaisesRegex(ExecutionRefused, '另一个实例'): self.execute(p)
        finally: lock.release(handle)
        self.assertExists(p['operations'][0]['source'])

    def test_original_position_occupied_stops_before_files_or_earlier_batch(self):
        self.file('first.pdf'); p1 = self.plan(['first.pdf']); self.execute(p1)
        session = OperationSession(); session.track(self.sandbox.settings, p1['plan_id'])
        self.file('second.pdf'); self.sandbox.add_dir('empty'); p2 = self.plan(['second.pdf', 'empty']); self.execute(p2)
        session.track(self.sandbox.settings, p2['plan_id']); self.sandbox.add_dir('empty')
        result = session.return_to(0, undo_plan)
        self.assertFalse(result.reached); self.assertEqual((result.stopped_at, result.completed), (2, []))
        self.assertExists(p1['operations'][0]['dest']); self.assertExists(p2['operations'][0]['dest'])
        self.assertEqual(session.head, 2)

    def test_external_file_change_blocks_chain_after_newer_batch_restored(self):
        self.file('first.pdf'); p1 = self.plan(['first.pdf']); self.execute(p1)
        session = OperationSession(); session.track(self.sandbox.settings, p1['plan_id'])
        self.sandbox.add_dir('empty'); p2 = self.plan(['empty']); self.execute(p2)
        session.track(self.sandbox.settings, p2['plan_id'])
        Path(p1['operations'][0]['dest']).write_bytes(b'EXTERNAL CHANGE')
        r = session.return_to(0, undo_plan)
        self.assertFalse(r.reached); self.assertEqual(r.completed, [2]); self.assertEqual(r.stopped_at, 1)
        self.assertEqual(r.restored_dirs, 1); self.assertNotExists(p1['operations'][0]['source'])

    def test_three_batches_return_v1_then_v0_and_new_branch(self):
        session = OperationSession(); plans = []
        for i in range(1, 4):
            name = f'batch{i}.pdf'; self.file(name)
            p = self.plan([name]); self.execute(p); session.track(self.sandbox.settings, p['plan_id']); plans.append(p)
        immutable = [self.frozen(p) for p in plans]
        r = session.return_to(1, undo_plan)
        self.assertEqual((r.completed, r.restored, r.reached), ([3, 2], 2, True))
        self.file('new.pdf'); p = self.plan(['new.pdf']); self.execute(p)
        step = session.track(self.sandbox.settings, p['plan_id']); self.assertEqual((step.version, step.parent), (4, 1))
        r = session.return_to(0, undo_plan); self.assertEqual((r.completed, r.restored, r.reached), ([4, 1], 2, True))
        self.assertEqual([self.frozen(p) for p in plans], immutable)
        for p in plans + [p]: self.roundtrip(p)

    def test_cross_source_classification_and_remaining_batches_return_v0(self):
        session = OperationSession()
        self.file('first.pdf'); p1 = build_plan(self.sandbox.settings).plan
        write_plan(self.sandbox.settings, p1); write_confirmation(self.sandbox.settings, p1['plan_id'], p1['plan_hash'])
        self.execute(p1); session.track(self.sandbox.settings, p1['plan_id'])
        other = self.sandbox.root/'Other'; other.mkdir(); (other/'empty').mkdir()
        (other/'second.csv').write_bytes(b'ARTIFICIAL:second.csv')
        settings = selected_settings(self.sandbox.settings, str(other))
        p2 = self.plan(['second.csv', 'empty'], settings=settings)
        execute_plan(settings, p2['plan_id'], p2['plan_hash']); session.track(settings, p2['plan_id'])
        r = session.return_to(0, undo_plan)
        self.assertEqual((r.completed, r.restored, r.restored_dirs, r.reached), ([2, 1], 2, 1, True))
        self.roundtrip(p1); self.roundtrip(p2, settings=settings)

    def test_plan_collision_new_unpublished_batch_preserves_old_bytes(self):
        self.file('a.pdf'); p1 = self.plan(['a.pdf'], plan_id='plan-collision')
        immutable = self.frozen(p1)
        p2 = build_remaining_plan(self.sandbox.settings, selection=['a.pdf'], plan_id='plan-collision').plan
        write_plan(self.sandbox.settings, p2)
        self.assertNotEqual(p2['plan_id'], p1['plan_id']); self.assertNotEqual(p2['plan_hash'], p1['plan_hash'])
        self.assertEqual(self.frozen(p1), immutable)
        self.assertIn('/' + p2['plan_id'] + '/', p2['operations'][0]['dest'])

    def test_forged_permissions_refused_even_with_recomputed_hash(self):
        self.file('a.pdf'); self.sandbox.add_dir('empty'); p = self.plan(['a.pdf', 'empty'], confirm=False)
        changes = [lambda q: q.update(downloads_dir=str(self.sandbox.root)),
                   lambda q: q['remaining_selection'].clear(),
                   lambda q: q['operations'][0].update(dest=q['target_root'] + '/elsewhere/a.pdf'),
                   lambda q: q['directory_operations'][0].update(preview_nonempty=True),
                   lambda q: q['dir_identities']['source_dirs'].pop('empty'),
                   lambda q: q['remaining_candidates'][0].update(source=q['downloads_dir'] + '/合同.pdf'),
                   lambda q: q.update(schema_version=3)]
        for change in changes:
            q = copy.deepcopy(p); change(q); q['plan_hash'] = compute_plan_hash(q)
            self.sandbox.plan_file(q['plan_id']).write_text(json.dumps(q))
            with self.assertRaises((PlanError, ExecutionRefused)):
                write_confirmation(self.sandbox.settings, q['plan_id'], q['plan_hash'])
            with self.assertRaises((PlanError, ExecutionRefused)): self.execute(q)
        self.assertExists(p['operations'][0]['source']); self.assertExists(p['directory_operations'][0]['source'])

    def test_wrong_source_cannot_execute_archived_plan(self):
        self.file('a.pdf'); p = self.plan(['a.pdf'])
        other = self.sandbox.root/'Other'; other.mkdir()
        settings = selected_settings(self.sandbox.settings, str(other))
        with self.assertRaises((PlanError, ExecutionRefused)):
            execute_plan(settings, p['plan_id'], p['plan_hash'])
        self.assertExists(p['operations'][0]['source'])

    def test_file_permission_failure_stops_later_directory_batch(self):
        self.file('a.pdf'); self.file('b.pdf'); self.sandbox.add_dir('empty')
        p = self.plan(['a.pdf', 'b.pdf', 'empty'])
        original = AtomicMover.rename_excl_at
        def denied(mover, sfd, src, dfd, dst):
            if src == 'a.pdf': raise PermissionError(errno.EACCES, 'artificial denial')
            return original(mover, sfd, src, dfd, dst)
        with patch.object(AtomicMover, 'rename_excl_at', new=denied): r = self.execute(p)
        self.assertTrue(r.stopped); self.assertEqual((r.moved, r.failed, r.not_attempted, r.archived_dirs), (0, 1, 1, 0))
        self.assertExists(self.sandbox.downloads/'b.pdf'); self.assertExists(self.sandbox.downloads/'empty')

    def test_source_parent_identity_replacement_without_link_is_refused(self):
        self.file('group/a.pdf'); p = self.plan(['group/a.pdf'], scope=['group'])
        os.rename(self.sandbox.downloads/'group', self.sandbox.downloads/'held')
        self.sandbox.add_dir('group'); self.file('group/a.pdf')
        with self.assertRaises((PlanError, ExecutionRefused, OSError)): self.execute(p)
        self.assertNotExists(p['operations'][0]['dest']); self.assertExists(self.sandbox.downloads/'held/a.pdf')

    def test_missing_directory_identity_key_or_conflicting_nfd_alias_is_refused(self):
        raw = 'Cafe' + chr(0x0301); name = unicodedata.normalize('NFC', raw)
        self.file(raw + '/a.pdf'); p = self.plan([name + '/a.pdf'], scope=[raw], confirm=False)
        p['dir_identities']['source_dirs'][raw] = [0, 0]
        p['plan_hash'] = compute_plan_hash(p); self.sandbox.plan_file(p['plan_id']).write_text(json.dumps(p))
        with self.assertRaises(PlanError): write_confirmation(self.sandbox.settings, p['plan_id'], p['plan_hash'])
        self.assertExists(self.sandbox.downloads/raw/'a.pdf')

    def test_target_parent_replaced_after_first_file_stops_unknown_chain(self):
        self.file('a.pdf'); self.file('b.pdf'); p = self.plan(['a.pdf', 'b.pdf'])
        def hook(event, op):
            if event == 'before_move' and op['op_id'] == 'op-0002':
                parent = Path(op['dest']).parent
                os.rename(parent, str(parent) + '-held'); parent.mkdir()
        r = self.execute(p, hook=hook)
        self.assertTrue(r.stopped); self.assertEqual((r.moved, r.failed), (1, 1))
        self.assertExists(p['operations'][1]['source'])
        session = OperationSession(); session.track(self.sandbox.settings, p['plan_id'])
        r = session.return_to(0, undo_plan); self.assertFalse(r.reached)

    def test_layout_intent_without_persisted_identity_never_rebases_recovery(self):
        self.sandbox.add_dir('empty'); p = self.plan(['empty'])
        def hook(event, op):
            if event == 'after_layout': raise SimulatedExit()
        with self.assertRaises(SimulatedExit): self.execute(p, hook=hook)
        immutable = self.frozen(p)
        log = Path(self.sandbox.journal(p['plan_id']).generation_file(1)).read_bytes()
        with self.assertRaises((PlanError, ExecutionRefused)): recover_execution(self.sandbox.settings, p['plan_id'])
        self.assertEqual(self.frozen(p), immutable)
        self.assertEqual(Path(self.sandbox.journal(p['plan_id']).generation_file(1)).read_bytes(), log)
        self.assertExists(p['directory_operations'][0]['source'])

    def test_session_lock_busy_stops_and_can_retry_without_nested_lock(self):
        self.sandbox.add_dir('empty'); p = self.plan(['empty']); self.execute(p)
        session = OperationSession(); session.track(self.sandbox.settings, p['plan_id'])
        lock = GlobalLock(self.sandbox.settings); handle = lock.acquire(kind='test', action='held')
        try:
            r = session.return_to(0, undo_plan)
            self.assertFalse(r.reached); self.assertEqual(r.stopped_at, 1)
            self.assertExists(p['directory_operations'][0]['dest'])
        finally: lock.release(handle)
        r = session.return_to(0, undo_plan); self.assertTrue(r.reached, r); self.roundtrip(p)

    def test_forged_confirmation_version_cannot_enable_new_authority(self):
        self.file('a.pdf'); p = self.plan(['a.pdf'])
        path = Path(confirmation_path(self.sandbox.settings, p['plan_id']))
        record = json.loads(path.read_text()); record['plan_schema_version'] = 3; path.write_text(json.dumps(record))
        with self.assertRaises(ExecutionRefused): self.execute(p)
        self.assertExists(p['operations'][0]['source'])

    def test_cli_discovery_default_no_plan_and_mixed_roundtrip(self):
        self.file('report.pdf'); self.sandbox.add_dir('empty')
        def cli(*args):
            r = subprocess.run([sys.executable, '-m', 'doc_selecter', *args],
                cwd=Path(__file__).resolve().parents[1], env=dict(os.environ), capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr); return r.stdout
        output = cli('remaining-plan')
        self.assertIn('默认不勾选', output)
        self.assertEqual(list((self.sandbox.state/'plans').glob('*.json')), [])
        cli('remaining-plan', '--all')
        paths = list((self.sandbox.state/'plans').glob('*.json')); self.assertEqual(len(paths), 1)
        p = json.loads(paths[0].read_text()); cli('confirm', '--plan', p['plan_id'])
        immutable = self.frozen(p)
        cli('execute', '--plan', p['plan_id'], '--confirm', p['plan_hash'])
        cli('recover', '--plan', p['plan_id']); cli('undo', '--plan', p['plan_id'], '--confirm')
        self.assertEqual(self.frozen(p), immutable); self.roundtrip(p)

    def test_cli_directory_only_independent_selected_batch(self):
        self.sandbox.add_dir('chosen'); self.sandbox.add_dir('not-chosen')
        repo = Path(__file__).resolve().parents[1]
        def cli(*args):
            r = subprocess.run([sys.executable, '-m', 'doc_selecter', *args], cwd=repo,
                env=dict(os.environ), capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cli('remaining-plan', '--select', 'chosen')
        paths = list((self.sandbox.state/'plans').glob('*.json')); self.assertEqual(len(paths), 1)
        p = json.loads(paths[0].read_text()); self.assertEqual(p['operations'], [])
        cli('confirm', '--plan', p['plan_id']); cli('execute', '--plan', p['plan_id'], '--confirm', p['plan_hash'])
        self.assertExists(self.sandbox.downloads/'not-chosen'); self.assertNotExists(self.sandbox.downloads/'chosen')
        cli('undo', '--plan', p['plan_id'], '--confirm'); self.roundtrip(p)

    def frozen_old(self, commit, *, version, interrupted=False):
        """Generate history from hash-verified frozen source, in a separate process."""
        repo = Path(__file__).resolve().parents[1]
        frozen = self.sandbox.root/f'frozen-v{version}'; frozen.mkdir()
        from tests.legacy_material import fixture_bytes, fixture_paths
        names = fixture_paths(commit)
        for name in names:
            if not name.endswith('.py'): continue
            path = frozen/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fixture_bytes(commit, name))
        self.file('group/deep/a.pdf')
        script = '''import json
from doc_selecter.config import load_settings
from doc_selecter.planner import build_plan,write_plan,write_confirmation
from doc_selecter.executor import execute_plan
from doc_selecter.journal import Journal
s=load_settings(); kw={'scope_dirs':['group'],'recursive':True}
VERSION = %d
if VERSION==3: kw['archive_emptied_dirs']=True
p=build_plan(s,**kw).plan
write_plan(s,p);write_confirmation(s,p['plan_id'],p['plan_hash'])
append=Journal.append
def interrupted(self,event,**kw):
    if event.get('event')=='result' and event.get('op_id')==('dir-0001' if VERSION==3 else 'op-0001'):
        raise SystemExit(17)
    return append(self,event,**kw)
if %s: Journal.append=interrupted
try: execute_plan(s,p['plan_id'],p['plan_hash'])
except SystemExit as e: assert e.code==17
print(json.dumps(p))
''' % (version, repr(interrupted))
        r = subprocess.run([sys.executable, '-c', script], cwd=frozen, env=dict(os.environ, PYTHONPATH=str(frozen)),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        p = json.loads(r.stdout); self.assertEqual(p['schema_version'], version)
        immutable = self.frozen(p); report = recover_execution(self.sandbox.settings, p['plan_id'])
        self.assertEqual(report['unknown'], [], report)
        u = undo_plan(self.sandbox.settings, p['plan_id'], confirm=True)
        self.assertEqual(u.undo_status, 'completed', u)
        self.assertIdentityMatches(p['operations'][0]['source'], p['operations'][0]['identity'])
        self.assertFileContent(p['operations'][0]['source'], b'ARTIFICIAL:group/deep/a.pdf')
        for op in p.get('directory_operations', []):
            self.assertEqual(list(dir_identity(op['source'])), [op['identity']['st_dev'], op['identity']['st_ino']])
            self.assertNotExists(op['dest'])
        self.assertNotExists(p['operations'][0]['dest']); self.assertEqual(self.frozen(p), immutable)
        self.assertTrue(all(Journal(self.sandbox.settings.state_dir,p['plan_id'],k).classify().healthy for k in ('execution','undo')))
        EVIDENCE.append(dict(route=self.id(), root=str(self.sandbox.root), fixed_commit=commit,
            version=version, interrupted=interrupted, original_identity_bytes=True, immutable=True, logs='healthy'))

    def test_fixed_v2_completed_history(self):
        self.frozen_old('2df1a18927a6653a886e146e54b7984ddc4912d1', version=2)

    def test_fixed_v2_interrupted_history(self):
        self.frozen_old('2df1a18927a6653a886e146e54b7984ddc4912d1', version=2, interrupted=True)

    def test_fixed_v3_completed_history(self):
        self.frozen_old('3b8b38029be069c6e36b394208c630c7f74e4437', version=3)

    def test_fixed_v3_interrupted_history(self):
        self.frozen_old('3b8b38029be069c6e36b394208c630c7f74e4437', version=3, interrupted=True)
