"""Step 2 public contracts. All material is synthetic and scoped by Sandbox.

Hooks deterministically inject filesystem changes or persistence faults; Darwin
rename calls remain real unless a test explicitly simulates EXDEV/capability.
"""
from __future__ import annotations
import copy
import errno
import io
import json
import os
import stat
import subprocess
import sys
import unicodedata
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from tests.harness import Sandbox, SandboxTestCase
from doc_selecter.atomic_move import AtomicMover, AtomicMoveUnavailable, MoveFailed, lstat_identity, dir_identity
from doc_selecter.executor import execute_plan, recover_execution, ExecutionRefused
from doc_selecter.gui_session import OperationSession
from doc_selecter.journal import Journal, JournalCorrupted, JournalWriteFailed, make_event, line_crc32
from doc_selecter.plan_hash import compute_plan_hash, verify_plan_hash
from doc_selecter.planner import build_plan, write_plan, write_confirmation, confirmation_path, PlanError
from doc_selecter.snapshot import read_snapshot, snapshot_path
from doc_selecter.undo import undo_plan, UndoRefused
from doc_selecter.watcher import NewFileWatcher

EVIDENCE=[]

class ArchiveTests(SandboxTestCase):
    def plan(self, scope=('group',), enabled=True, categories=None):
        p=build_plan(self.sandbox.settings,scope_dirs=list(scope),recursive=bool(scope),
                     archive_emptied_dirs=enabled,category_selection=categories).plan
        write_plan(self.sandbox.settings,p)
        write_confirmation(self.sandbox.settings,p['plan_id'],p['plan_hash'])
        return p

    def execute(self,p,hook=None):
        return execute_plan(self.sandbox.settings,p['plan_id'],p['plan_hash'],hook=hook)

    def immutable(self,p):
        return {str(path):path.read_bytes() for path in [self.sandbox.plan_file(p['plan_id']),Path(confirmation_path(self.sandbox.settings,p['plan_id']))]}

    def artifacts(self):
        # State phase and lock may legitimately report the refusal; trust artifacts may not change.
        state={}
        for folder in ('plans','confirmations','journals','undos'):
            root=self.sandbox.state/folder
            for path in root.rglob('*') if root.exists() else []:
                if path.is_file(): state[str(path)]=path.read_bytes()
        return state

    def frozen(self,p):
        return [(op,Path(op['source']).read_bytes()) for op in p['operations']], self.immutable(p)

    def roundtrip(self,p,original,immutable,expected_dirs):
        report=recover_execution(self.sandbox.settings,p['plan_id'])
        self.assertEqual(report['unknown'],[],report)
        undo=undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)
        self.assertEqual(undo.undo_status,'completed',undo)
        self.assertEqual(undo.restored,len(original))
        self.assertEqual(undo.restored_dirs,expected_dirs)
        for op,content in original:
            self.assertIdentityMatches(op['source'],op['identity'])
            self.assertFileContent(op['source'],content)
            self.assertIn(Path(op['source']).name,os.listdir(Path(op['source']).parent))
            self.assertNotExists(op['dest'])
        for op in p['directory_operations']:
            self.assertEqual(list(dir_identity(op['source'])),[op['identity']['st_dev'],op['identity']['st_ino']])
            self.assertTrue(stat.S_ISDIR(os.lstat(op['source']).st_mode))
            self.assertNotExists(op['dest'])
        self.assertEqual(self.immutable(p),immutable)
        logs={k:self.sandbox.journal(p['plan_id'],k).classify().classification for k in ('execution','undo')}
        self.assertEqual(logs,{'execution':'healthy','undo':'healthy'})
        EVIDENCE.append({'route':self.id(),'root':str(self.sandbox.root),'plan_id':p['plan_id'],'files':len(original),'directories':expected_dirs,'logs':logs,'identities_and_bytes_restored':True})

    def test_default_off_does_not_probe_directory_capability(self):
        self.sandbox.add_file('group/deep/report.pdf',b'off')
        p=self.plan(enabled=False);original,immutable=self.frozen(p)
        self.assertEqual(p['directory_operations'],[])
        with patch.object(AtomicMover,'probe_directory_capability',side_effect=AssertionError('must not probe')):
            self.assertEqual(self.execute(p).moved,1)
            self.roundtrip(p,original,immutable,0)
        self.assertExists(self.sandbox.downloads/'group/deep')

    def test_nested_original_objects_deep_first_restore_shallow_first(self):
        for name in ('group/deep/report.pdf','group/readme.md','second/deep/report.pdf'):
            self.sandbox.add_file(name,('ARTIFICIAL '+name).encode())
        p=self.plan(scope=('group','second'));original,immutable=self.frozen(p)
        result=self.execute(p)
        self.assertEqual((result.moved,result.archived_dirs,result.stopped),(3,4,False),result)
        events=self.sandbox.journal(p['plan_id']).read_events()[0]
        archive_ids=[e['op_id'] for e in events if e['event']=='result' and e.get('outcome')=='archived']
        depths={o['op_id']:o['rel_path'].count('/') for o in p['directory_operations']}
        self.assertEqual([depths[i] for i in archive_ids],sorted([depths[i] for i in archive_ids],reverse=True))
        self.roundtrip(p,original,immutable,4)
        events=self.sandbox.journal(p['plan_id'],'undo').read_events()[0]
        dirs=[e['op_id'] for e in events if e['event']=='undo_result' and e.get('op_kind')=='empty_directory']
        self.assertEqual([depths[i] for i in dirs],sorted([depths[i] for i in dirs]))
        first_file=next(i for i,e in enumerate(events) if e['op_kind']=='file')
        self.assertTrue(all(e['op_kind']=='empty_directory' for e in events[:first_file]))

    def test_top_level_six_subclasses_do_not_archive_source_root(self):
        for name in ('a.xlsx','b.pdf','c.docx','d.pptx','e.md','f.txt','g.png','h.csv'):
            self.sandbox.add_file(name,b'artificial:'+name.encode())
        p=self.plan(scope=());original,immutable=self.frozen(p)
        self.assertEqual(p['directory_operations'],[])
        self.assertEqual((self.execute(p).moved),8)
        self.roundtrip(p,original,immutable,0)

    def test_hidden_temporary_private_empty_unselected_members_excluded(self):
        for name in ('group/deep/report.pdf','group/.hidden','group/hold.tmp','group/private.txt','unselected/report.pdf'):
            self.sandbox.add_file(name,b'ARTIFICIAL')
        self.sandbox.add_dir('group/empty')
        p=self.plan();self.assertEqual([o['rel_path'] for o in p['directory_operations']],['group/deep'])
        result=self.execute(p);self.assertEqual(result.archived_dirs,1)
        for name in ('.hidden','hold.tmp','private.txt','empty'): self.assertExists(self.sandbox.downloads/'group'/name)
        self.assertExists(self.sandbox.downloads/'unselected/report.pdf')
        self.assertNotIn('private.txt',self.sandbox.plan_file(p['plan_id']).read_text())
        undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)

    def test_only_deep_selected_does_not_archive_transit_ancestors(self):
        self.sandbox.add_file('group/deep/report.pdf')
        p=self.plan(scope=('group/deep',))
        self.assertEqual([o['rel_path'] for o in p['directory_operations']],['group/deep'])
        result=self.execute(p);self.assertEqual(result.archived_dirs,1)
        self.assertExists(self.sandbox.downloads/'group')
        undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)

    def test_unicode_nested_roundtrip_and_independent_same_name_slots(self):
        name=unicodedata.normalize('NFD','Café')
        for rel in (name+'/data/report.pdf','group/data/report.pdf'): self.sandbox.add_file(rel,b'UNICODE')
        p=self.plan(scope=(name,'group'));original,immutable=self.frozen(p)
        self.assertEqual(len({o['dest'] for o in p['directory_operations']}),4)
        self.assertEqual(self.execute(p).archived_dirs,4)
        self.roundtrip(p,original,immutable,4)

    def test_partial_file_success_blocks_causal_ancestor(self):
        self.sandbox.add_file('group/good/report.pdf');self.sandbox.add_file('group/bad/report.pdf')
        conflict=self.sandbox.downloads/'下载分类/文档资料/group/bad/report.pdf'
        conflict.parent.mkdir(parents=True);conflict.write_bytes(b'EXTERNAL')
        before=lstat_identity(str(conflict)).as_dict()
        p=self.plan();r=self.execute(p)
        self.assertEqual((r.moved,r.skipped,r.archived_dirs,r.dirs_skipped),(1,1,1,2),r)
        self.assertIdentityMatches(conflict,before);self.assertFileContent(conflict,b'EXTERNAL')
        self.assertExists(self.sandbox.downloads/'group/bad/report.pdf')
        self.assertExists(self.sandbox.downloads/'group')
        u=undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)
        self.assertEqual((u.restored,u.restored_dirs,u.undo_status),(1,1,'completed'))

    def test_category_cancellation_keeps_remaining_directory(self):
        self.sandbox.add_file('group/report.pdf');self.sandbox.add_file('group/photo.png')
        p=self.plan(categories=['文档资料']);self.assertEqual(p['directory_operations'],[])
        self.assertEqual(self.execute(p).moved,1);self.assertExists(self.sandbox.downloads/'group/photo.png')
        undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)

    def test_external_deletion_is_not_successful_dependency(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();Path(p['operations'][0]['source']).unlink()
        before=self.artifacts()
        with self.assertRaises(PlanError):self.execute(p)
        self.assertEqual(self.artifacts(),before);self.assertExists(self.sandbox.downloads/'group')

    def test_external_add_before_directory_stage_including_hidden_is_not_moved(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan()
        def hook(event,op):
            if event=='before_move': (self.sandbox.downloads/'group/.arrived').write_bytes(b'ADDED')
        r=self.execute(p,hook);self.assertEqual((r.moved,r.archived_dirs,r.dirs_skipped),(1,0,1))
        self.assertExists(self.sandbox.downloads/'group/.arrived')

    def test_content_in_final_rename_window_moves_but_stops_without_safe_success(self):
        for rel in ('a/report.pdf','b/report.pdf'):self.sandbox.add_file(rel)
        p=self.plan(scope=('a','b'));calls=[]
        def hook(event,op):
            if event=='before_directory_move':
                calls.append(op['op_id']);Path(op['source'],'.arrived').write_bytes(b'RACE ARTIFICIAL')
        r=self.execute(p,hook)
        self.assertTrue(r.stopped);self.assertEqual((r.archived_dirs,r.dirs_failed,r.dirs_not_attempted),(0,1,1))
        self.assertEqual(len(calls),1);first=next(o for o in p['directory_operations'] if o['op_id']==calls[0])
        self.assertFileContent(Path(first['dest'])/'.arrived',b'RACE ARTIFICIAL')
        before=self.artifacts()
        with self.assertRaises(PlanError):recover_execution(self.sandbox.settings,p['plan_id'])
        with self.assertRaises(UndoRefused):undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)
        self.assertEqual(self.artifacts(),before)
        self.assertExists(self.sandbox.downloads/'b')
        session=OperationSession();step=session.track(self.sandbox.settings,p['plan_id']);self.assertEqual(step.uncertain_dirs,1)
        self.assertFalse(session.return_to(0,undo_plan).reached)

    def test_rehashed_fixed_slot_dependencies_authorization_and_identity_tampering(self):
        self.sandbox.add_file('group/deep/report.pdf');p=self.plan();original=self.immutable(p)
        mutations=[lambda x:x['archive'].__setitem__('enabled',False),
                   lambda x:x['archive'].__setitem__('relative_root','下载分类/已归档/other/空文件夹'),
                   lambda x:x['directory_operations'][0].__setitem__('dest',x['directory_operations'][0]['dest']+'x'),
                   lambda x:x['directory_operations'][0].__setitem__('depends_on',[]),
                   lambda x:x['directory_operations'][0].__setitem__('direct_members',[]),
                   lambda x:x['directory_operations'][0]['identity'].__setitem__('st_ino',0),
                   lambda x:x.__setitem__('selected_scope',['']),
                   lambda x:x['operations'][0].__setitem__('category','图片截图')]
        for mutate in mutations:
            bad=copy.deepcopy(p);mutate(bad);bad['plan_hash']=compute_plan_hash(bad)
            self.assertTrue(verify_plan_hash(bad))
            self.sandbox.plan_file(p['plan_id']).write_text(json.dumps(bad,ensure_ascii=False))
            before=self.artifacts()
            for fn in (lambda:write_confirmation(self.sandbox.settings,p['plan_id'],bad['plan_hash']),
                       lambda:execute_plan(self.sandbox.settings,p['plan_id'],bad['plan_hash']),
                       lambda:recover_execution(self.sandbox.settings,p['plan_id']),
                       lambda:undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)):
                with self.assertRaises((PlanError,ExecutionRefused,UndoRefused)):fn()
                self.assertEqual(self.artifacts(),before)
        for path,data in original.items():Path(path).write_bytes(data)
        for op in p['operations']:self.assertIdentityMatches(op['source'],op['identity'])

    def test_source_parent_replacement_or_link_refused_before_probe_and_logs(self):
        self.sandbox.add_file('group/deep/report.pdf');p=self.plan();path=self.sandbox.downloads/'group'
        saved=self.sandbox.root/'saved';path.rename(saved)
        for link in (False,True):
            path.symlink_to(saved,target_is_directory=True) if link else path.mkdir()
            before=self.artifacts()
            with patch('doc_selecter.executor.probe_capability') as probe:
                with self.assertRaises(PlanError):self.execute(p)
                probe.assert_not_called()
            self.assertEqual(self.artifacts(),before)
            path.unlink() if link else path.rmdir()
        saved.rename(path)

    def test_archived_directory_link_and_rehashed_alternate_slot_refused(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();self.assertEqual(self.execute(p).archived_dirs,1)
        op=p['directory_operations'][0];slot=Path(op['dest']);actual=slot.with_name('actual');slot.rename(actual);slot.symlink_to(actual,target_is_directory=True)
        for rehash in (False,True):
            bad=copy.deepcopy(p)
            if rehash:bad['directory_operations'][0]['dest']=str(actual);bad['plan_hash']=compute_plan_hash(bad)
            self.sandbox.plan_file(p['plan_id']).write_text(json.dumps(bad,ensure_ascii=False));before=self.artifacts()
            with patch.object(AtomicMover,'probe_directory_capability') as probe:
                with self.assertRaises(PlanError):recover_execution(self.sandbox.settings,p['plan_id'])
                with self.assertRaises(UndoRefused):undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)
                probe.assert_not_called()
            self.assertEqual(self.artifacts(),before)
            self.assertEqual(list(dir_identity(str(actual))),[op['identity']['st_dev'],op['identity']['st_ino']])

    def test_directory_capability_unavailable_refuses_whole_batch_before_files(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();before=self.artifacts()
        with patch.object(AtomicMover,'probe_directory_capability',side_effect=AtomicMoveUnavailable('SIMULATED')):
            with self.assertRaises(ExecutionRefused):self.execute(p)
        self.assertEqual(self.artifacts(),before);self.assertNotExists(self.sandbox.downloads/'下载分类')

    def test_real_directory_destination_conflicts_never_overwrite(self):
        # Targets appear after layout admission, exactly in the primitive window.
        for kind in ('file','empty','nonempty'):
            with self.subTest(kind=kind):
                nested=Sandbox();nested.activate()
                prior=self.sandbox;self.sandbox=nested
                try:
                    self.sandbox.add_file('group/report.pdf');p=self.plan();objects=[]
                    def hook(event,op):
                        if event=='before_directory_move':
                            dest=Path(op['dest'])
                            if kind=='file':dest.write_bytes(b'CONFLICT')
                            else:
                                dest.mkdir()
                                if kind=='nonempty':(dest/'item').write_bytes(b'CONFLICT')
                            objects.append((dest,lstat_identity(str(dest)).as_dict()))
                    r=self.execute(p,hook);self.assertEqual(r.archived_dirs,0)
                    self.assertExists(self.sandbox.downloads/'group');self.assertEqual(list((self.sandbox.downloads/'group').iterdir()),[])
                    for dest,ident in objects:self.assertIdentityMatches(dest,ident)
                    if kind=='file':self.assertFileContent(objects[0][0],b'CONFLICT')
                    elif kind=='nonempty':self.assertFileContent(objects[0][0]/'item',b'CONFLICT')
                finally:self.sandbox=prior;nested.close()

    def test_exdev_simulation_does_not_copy_delete_or_archive(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();real=AtomicMover.rename_excl_at
        def rename(mover,sfd,sname,dfd,dname):
            if sname=='group':raise MoveFailed(errno.EXDEV,'SIMULATED EXDEV')
            return real(mover,sfd,sname,dfd,dname)
        with patch.object(AtomicMover,'rename_excl_at',rename):r=self.execute(p)
        self.assertEqual((r.archived_dirs,r.dirs_skipped),(0,1));self.assertExists(self.sandbox.downloads/'group')

    def test_directory_result_failure_recover_and_undo(self):
        self.sandbox.add_file('group/deep/report.pdf',b'INTERRUPTED');p=self.plan();original,immutable=self.frozen(p)
        tripped=[]
        def hook(event,op):
            if event=='before_directory_result' and not tripped:tripped.append(op['op_id']);raise JournalWriteFailed('SIMULATED RESULT FAILURE')
        r=self.execute(p,hook);self.assertTrue(r.stopped)
        fold=self.sandbox.journal(p['plan_id']).fold()[0];self.assertIsNone(fold[tripped[0]].get('result'))
        report=recover_execution(self.sandbox.settings,p['plan_id']);self.assertIn(tripped[0],report['recovered'])
        self.roundtrip(p,original,immutable,1)

    def test_directory_intent_failure_zero_directory_moves(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();real=Journal.append
        def append(journal,payload,**kwargs):
            if payload.get('event')=='intent' and payload.get('op_id','').startswith('dir-'):raise JournalWriteFailed('SIMULATED INTENT FAILURE')
            return real(journal,payload,**kwargs)
        with patch.object(Journal,'append',append):r=self.execute(p)
        self.assertTrue(r.stopped);self.assertEqual(r.archived_dirs,0);self.assertExists(self.sandbox.downloads/'group')
        self.assertEqual(recover_execution(self.sandbox.settings,p['plan_id'])['unknown'],[])
        self.assertEqual(undo_plan(self.sandbox.settings,p['plan_id'],confirm=True).restored,1)

    def test_layout_created_without_identity_record_stays_untrusted(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();tripped=[]
        def hook(event,op):
            if event=='after_layout':tripped.append(op['dest']);raise JournalWriteFailed('SIMULATED MKDIR INTERRUPTION')
        r=self.execute(p,hook);self.assertEqual(r.moved,0);self.assertTrue(r.stopped);self.assertExists(tripped[0])
        before=self.artifacts()
        with self.assertRaisesRegex(PlanError,'未持久化绑定'):recover_execution(self.sandbox.settings,p['plan_id'])
        with self.assertRaises(UndoRefused):undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)
        self.assertEqual(self.artifacts(),before)

    def test_layout_intent_only_absent_container_recover_does_not_create_it(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan()
        def hook(event,op):
            if event=='before_layout':raise JournalWriteFailed('SIMULATED BEFORE MKDIR')
        self.assertTrue(self.execute(p,hook).stopped)
        self.assertEqual(recover_execution(self.sandbox.settings,p['plan_id'])['unknown'],[])
        self.assertNotExists(self.sandbox.downloads/'下载分类')
        with self.assertRaises(ExecutionRefused):self.execute(p)

    def test_archive_intent_before_rename_recovery_does_not_retry(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();real=AtomicMover.rename_excl_at
        def rename(mover,sfd,sname,dfd,dname):
            if sname=='group':raise KeyboardInterrupt('SIMULATED PROCESS INTERRUPTION')
            return real(mover,sfd,sname,dfd,dname)
        with patch.object(AtomicMover,'rename_excl_at',rename):
            with self.assertRaises(KeyboardInterrupt):self.execute(p)
        report=recover_execution(self.sandbox.settings,p['plan_id']);self.assertEqual(report['recovered'],[])
        self.assertExists(self.sandbox.downloads/'group');self.assertEqual(undo_plan(self.sandbox.settings,p['plan_id'],confirm=True).restored,1)

    def test_directory_undo_after_rename_interruption_recovers_without_repeat(self):
        self.sandbox.add_file('group/deep/report.pdf',b'UNDO INTERRUPTION');p=self.plan();original,immutable=self.frozen(p);self.execute(p)
        tripped=[]
        def hook(event,op):
            if event=='after_directory_restore' and not tripped:tripped.append(op['op_id']);raise JournalWriteFailed('SIMULATED UNDO RESULT LOSS')
        with self.assertRaises(UndoRefused):undo_plan(self.sandbox.settings,p['plan_id'],confirm=True,hook=hook)
        self.assertEqual(recover_execution(self.sandbox.settings,p['plan_id'])['unknown'],[])
        self.roundtrip(p,original,immutable,1)
        events=self.sandbox.journal(p['plan_id'],'undo').read_events()[0]
        self.assertEqual(sum(e['op_id']==tripped[0] and e['event']=='undo_intent' for e in events),1)

    def test_undo_directory_conflict_blocks_files_and_earlier_batch(self):
        session=OperationSession()
        self.sandbox.add_file('first.pdf');first=self.plan(scope=());self.execute(first);session.track(self.sandbox.settings,first['plan_id'])
        self.sandbox.add_file('group/report.pdf');p=self.plan();self.execute(p);session.track(self.sandbox.settings,p['plan_id'])
        (self.sandbox.downloads/'group').mkdir();before=self.artifacts()
        report=session.return_to(0,undo_plan);self.assertFalse(report.reached);self.assertEqual(report.stopped_at,2)
        self.assertEqual(self.artifacts(),before)
        self.assertNotExists(first['operations'][0]['source']);self.assertExists(first['operations'][0]['dest'])
        self.assertNotExists(p['operations'][0]['source'])

    def test_cross_source_session_return_v0_freezes_original_settings(self):
        session=OperationSession();plans=[];sandboxes=[];prior=self.sandbox
        try:
            for i in range(2):
                box=Sandbox();box.activate();self.sandbox=box;sandboxes.append(box)
                box.add_file('group/deep/report.pdf',b'CROSS SOURCE '+bytes([i]));p=self.plan();original,immutable=self.frozen(p);plans.append((box,p,original,immutable))
                self.assertEqual(self.execute(p).archived_dirs,2);session.track(box.settings,p['plan_id'])
            report=session.return_to(0,undo_plan);self.assertTrue(report.reached,report);self.assertEqual((report.restored,report.restored_dirs),(2,4))
            for box,p,original,immutable in plans:
                self.sandbox=box
                for op,content in original:self.assertIdentityMatches(op['source'],op['identity']);self.assertFileContent(op['source'],content);self.assertNotExists(op['dest'])
                for op in p['directory_operations']:self.assertEqual(list(dir_identity(op['source'])),[op['identity']['st_dev'],op['identity']['st_ino']]);self.assertNotExists(op['dest'])
                self.assertEqual(self.immutable(p),immutable)
                self.assertTrue(all(box.journal(p['plan_id'],k).classify().healthy for k in ('execution','undo')))
                EVIDENCE.append({'route':'cross-source-V0','root':str(box.root),'plan_id':p['plan_id'],'originals_restored':True})
        finally:
            self.sandbox=prior
            for box in reversed(sandboxes):box.close()

    def test_archive_region_excluded_by_scanner_and_new_file_notification(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();self.execute(p)
        self.sandbox.add_file('下载分类/已归档/manual/report.pdf')
        self.assertEqual(build_plan(self.sandbox.settings,archive_emptied_dirs=True).plan['operations'],[])
        with self.assertRaises(PlanError):build_plan(self.sandbox.settings,scope_dirs=['下载分类'],recursive=True,archive_emptied_dirs=True)
        clock=[0.];watcher=NewFileWatcher(clock=lambda:clock[0]);watcher.enable()
        for time in (0.,6.):
            clock[0]=time;report=watcher.check_once(downloads_dir=str(self.sandbox.downloads),target_parent_name='下载分类',scope_dirs=['下载分类'],recursive=True)
        self.assertEqual(report.new_files,[])

    def test_double_log_corruption_refused_before_probe_repairs_and_snapshot(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();self.execute(p)
        journal=self.sandbox.journal(p['plan_id'],'undo');journal.append(make_event('undo_intent',p['directory_operations'][0]['op_id'],**{'from':p['directory_operations'][0]['dest'],'to':p['directory_operations'][0]['source'],'expected_identity':p['directory_operations'][0]['identity']}))
        file=Path(journal.generation_file(1));file.write_bytes(b'NOT JSON\n'+file.read_bytes());before=self.artifacts()
        with patch.object(AtomicMover,'probe_directory_capability') as probe:
            self.assertEqual(recover_execution(self.sandbox.settings,p['plan_id'])['phase'],'corrupted')
            with self.assertRaises(UndoRefused):undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)
            probe.assert_not_called()
        self.assertEqual(self.artifacts(),before)

    def test_plan_confirmation_immutable_snapshot_is_not_authorization(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();self.execute(p)
        snap=Path(snapshot_path(self.sandbox.settings,p['plan_id']));data=json.loads(snap.read_text());data['directory_entries']=[];data['undo_status']='completed';snap.write_text(json.dumps(data))
        u=undo_plan(self.sandbox.settings,p['plan_id'],confirm=True);self.assertEqual((u.restored,u.restored_dirs),(1,1))
        with self.assertRaises(ExecutionRefused):self.execute(p)

    def test_cli_archive_preview_confirmation_execute_recover_undo(self):
        self.sandbox.add_file('group/deep/report.pdf',b'CLI ARTIFICIAL')
        from doc_selecter.cli import main
        def command(args):
            output=io.StringIO()
            with redirect_stdout(output),redirect_stderr(output):code=main(args)
            self.assertEqual(code,0,output.getvalue());return output.getvalue()
        preview=command(['plan','--include-subfolder','group','--archive-emptied-dirs']);self.assertIn('条件目录归档',preview)
        p=json.loads(next(self.sandbox.state.joinpath('plans').glob('*.json')).read_text());original,immutable_files=self.frozen_without_confirmation(p)
        text=command(['confirm','--plan',p['plan_id']]);self.assertIn('暂停下载',text)
        immutable=self.immutable(p)
        text=command(['execute','--plan',p['plan_id'],'--confirm',p['plan_hash']]);self.assertIn('目录归档',text)
        command(['recover','--plan',p['plan_id']]);text=command(['undo','--plan',p['plan_id'],'--confirm']);self.assertIn('目录还原',text)
        for op,content in original:self.assertIdentityMatches(op['source'],op['identity']);self.assertFileContent(op['source'],content)
        self.assertEqual(self.immutable(p),immutable)

    def frozen_without_confirmation(self,p):
        return [(op,Path(op['source']).read_bytes()) for op in p['operations']],None

    def test_frozen_real_step1_v2_completed_and_interrupted_roundtrip(self):
        template=json.loads(Path(__file__).with_name('fixtures').joinpath('step1_v2_plan.json').read_text())
        self.assertEqual((template['schema_version'],template['rules_version']),(2,'milestone5-document-subcategories-r1'))
        for interrupted in (False,True):
            box=Sandbox();box.activate()
            try:
                payload=b'FROZEN STEP1 ARTIFICIAL';source=box.add_file('step1-fixture.pdf',payload)
                # Instantiate the frozen format BEFORE writing any immutable history.
                p=json.loads(json.dumps(template).replace('<SOURCE>',str(box.downloads)))
                p['operations'][0]['identity']=lstat_identity(str(source)).as_dict()
                root=list(dir_identity(str(box.downloads)));p['dir_identities']['downloads_dir']=root;p['dir_identities']['source_dirs']['']=root
                p['plan_hash']=compute_plan_hash(p);self.assertTrue(verify_plan_hash(p));write_plan(box.settings,p)
                path=Path(confirmation_path(box.settings,p['plan_id']));path.parent.mkdir(parents=True);path.write_text(json.dumps({'schema_version':1,'plan_id':p['plan_id'],'confirmed_plan_hash':p['plan_hash'],'confirmed_at':'frozen artificial'}))
                immutable={str(box.plan_file(p['plan_id'])):box.plan_file(p['plan_id']).read_bytes(),str(path):path.read_bytes()}
                op=p['operations'][0];journal=box.journal(p['plan_id']);journal.append(make_event('intent',op['op_id'],source=op['source'],dest=op['dest'],identity=op['identity'],category=op['category']))
                Path(op['dest']).parent.mkdir(parents=True);AtomicMover().rename_excl(op['source'],op['dest'])
                if not interrupted:journal.append(make_event('result',op['op_id'],outcome='moved',reason_code='OK',dest_identity=op['identity']))
                report=recover_execution(box.settings,p['plan_id']);self.assertEqual(report['unknown'],[])
                if interrupted:self.assertIn(op['op_id'],report['recovered'])
                u=undo_plan(box.settings,p['plan_id'],confirm=True);self.assertEqual(u.restored,1)
                self.assertIdentityMatches(op['source'],op['identity']);self.assertFileContent(op['source'],payload);self.assertNotExists(op['dest'])
                for path,bytes_ in immutable.items():self.assertEqual(Path(path).read_bytes(),bytes_)
                self.assertTrue(all(box.journal(p['plan_id'],k).classify().healthy for k in ('execution','undo')))
                with self.assertRaises(ExecutionRefused):execute_plan(box.settings,p['plan_id'],p['plan_hash'])
                with self.assertRaises(PlanError):write_confirmation(box.settings,p['plan_id'],p['plan_hash'])
                EVIDENCE.append({'route':'frozen-step1-v2','interrupted':interrupted,'root':str(box.root),'five_fields_bytes_and_history_unchanged':True})
            finally:box.close()

    def test_fsync_failure_after_directory_rename_records_unknown_and_stops(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();real=os.fsync;armed=[False];tripped=[]
        def hook(event,op):
            if event=='after_directory_move':armed[0]=True
        def fsync(fd):
            if armed[0] and stat.S_ISDIR(os.fstat(fd).st_mode) and not tripped:
                tripped.append(fd);raise OSError(errno.EIO,'SIMULATED DIR FSYNC')
            return real(fd)
        with patch('os.fsync',fsync):r=self.execute(p,hook)
        self.assertTrue(r.stopped);self.assertEqual(r.archived_dirs,0);self.assertEqual(r.dirs_failed,1)
        slot=self.sandbox.journal(p['plan_id']).fold()[0][p['directory_operations'][0]['op_id']]
        self.assertEqual(slot['result']['outcome'],'unknown_needs_review')
        before=self.artifacts()
        with self.assertRaises(PlanError):recover_execution(self.sandbox.settings,p['plan_id'])
        self.assertEqual(self.artifacts(),before)

    def test_snapshot_failure_after_archiving_recovers_from_logs(self):
        self.sandbox.add_file('group/report.pdf',b'SNAPSHOT FAULT');p=self.plan();original,immutable=self.frozen(p)
        with patch('doc_selecter.snapshot.write_snapshot',side_effect=OSError(errno.ENOSPC,'SIMULATED SNAPSHOT')):r=self.execute(p)
        self.assertTrue(r.stopped);self.assertEqual((r.moved,r.archived_dirs),(1,1))
        self.assertIsNone(read_snapshot(self.sandbox.settings,p['plan_id']))
        self.roundtrip(p,original,immutable,1)

    def test_layout_manifest_publish_failure_does_not_move_user_object(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan()
        real=Journal._write_manifest;tripped=[]
        def publish(journal,manifest):
            if not tripped:tripped.append(journal.plan_id);raise OSError(errno.ENOSPC,'SIMULATED MANIFEST')
            return real(journal,manifest)
        with patch.object(Journal,'_write_manifest',publish):r=self.execute(p)
        self.assertEqual((r.moved,r.archived_dirs),(0,0));self.assertTrue(r.stopped)
        self.assertIdentityMatches(p['operations'][0]['source'],p['operations'][0]['identity'])
        self.assertNotExists(self.sandbox.downloads/'下载分类')
        report=recover_execution(self.sandbox.settings,p['plan_id']);self.assertEqual(report['unknown'],[])

    def test_recover_directory_intent_with_both_locations_preserves_all_evidence(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();real=AtomicMover.rename_excl_at
        def rename(mover,sfd,sname,dfd,dname):
            if sname=='group':raise KeyboardInterrupt('SIMULATED PROCESS STOP')
            return real(mover,sfd,sname,dfd,dname)
        with patch.object(AtomicMover,'rename_excl_at',rename):
            with self.assertRaises(KeyboardInterrupt):self.execute(p)
        directory=p['directory_operations'][0];Path(directory['dest']).mkdir();before=self.artifacts()
        report=recover_execution(self.sandbox.settings,p['plan_id']);self.assertEqual(report['unknown'],[directory['op_id']])
        with patch('doc_selecter.undo.probe_capability') as probe:
            with self.assertRaises(UndoRefused):undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)
            probe.assert_not_called()
        self.assertEqual(self.artifacts(),before)
        self.assertExists(directory['source']);self.assertExists(directory['dest'])

    def test_recover_directory_intent_with_neither_location_refuses_without_search(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();real=AtomicMover.rename_excl_at
        def rename(mover,sfd,sname,dfd,dname):
            if sname=='group':raise KeyboardInterrupt('SIMULATED PROCESS STOP')
            return real(mover,sfd,sname,dfd,dname)
        with patch.object(AtomicMover,'rename_excl_at',rename):
            with self.assertRaises(KeyboardInterrupt):self.execute(p)
        directory=p['directory_operations'][0];saved=self.sandbox.root/'saved';Path(directory['source']).rename(saved);before=self.artifacts()
        with self.assertRaises(PlanError):recover_execution(self.sandbox.settings,p['plan_id'])
        self.assertEqual(self.artifacts(),before);self.assertEqual(list(dir_identity(str(saved))),[directory['identity']['st_dev'],directory['identity']['st_ino']])

    def test_slot_parent_replacement_refused_before_recovery_or_undo_mutation(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();self.execute(p)
        parent=Path(p['directory_operations'][0]['dest']).parent;saved=self.sandbox.root/'saved';parent.rename(saved);parent.mkdir()
        before=self.artifacts()
        with self.assertRaisesRegex(PlanError,'身份'):recover_execution(self.sandbox.settings,p['plan_id'])
        with self.assertRaises(UndoRefused):undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)
        self.assertEqual(self.artifacts(),before);self.assertExists(saved/'group')

    def test_restored_parent_unknown_content_blocks_child_and_files(self):
        self.sandbox.add_file('group/deep/report.pdf');p=self.plan();self.execute(p)
        def hook(event,op):
            if event=='after_directory_restore':Path(op['source'],'.arrived').write_bytes(b'EXTERNAL AFTER RESTORE')
        with self.assertRaises(UndoRefused):undo_plan(self.sandbox.settings,p['plan_id'],confirm=True,hook=hook)
        before=self.artifacts()
        with self.assertRaises(PlanError):recover_execution(self.sandbox.settings,p['plan_id'])
        self.assertEqual(self.artifacts(),before);self.assertNotExists(p['operations'][0]['source'])

    def test_directory_undo_intent_continuation_reuses_intent_after_explicit_resume(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();self.execute(p)
        def hook(event,op):
            if event=='before_directory_restore':raise JournalWriteFailed('SIMULATED BEFORE RESTORE')
        with self.assertRaises(UndoRefused):undo_plan(self.sandbox.settings,p['plan_id'],confirm=True,hook=hook)
        before=self.artifacts()
        with self.assertRaises(UndoRefused):undo_plan(self.sandbox.settings,p['plan_id'],confirm=True,resume=False)
        self.assertEqual(self.artifacts(),before)
        self.assertEqual(recover_execution(self.sandbox.settings,p['plan_id'])['unknown'],[])
        u=undo_plan(self.sandbox.settings,p['plan_id'],confirm=True,resume=True);self.assertEqual((u.restored,u.restored_dirs),(1,1))
        events=self.sandbox.journal(p['plan_id'],'undo').read_events()[0]
        self.assertEqual(sum(e['event']=='undo_intent' and e['op_kind']=='empty_directory' for e in events),1)

    def test_result_payload_tampering_with_valid_crc_is_rejected_without_repairs(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();self.execute(p)
        journal=self.sandbox.journal(p['plan_id']);path=Path(journal.generation_file(1));original=path.read_bytes()
        for change in ('op_kind','outcome','plan_hash','identity'):
            events=[json.loads(line) for line in original.splitlines()]
            result=next(e for e in events if e['event']=='result' and e['op_kind']=='empty_directory')
            if change=='op_kind':result['op_kind']='file'
            elif change=='outcome':result['outcome']='invented'
            elif change=='plan_hash':result['plan_hash']='sha256:'+'0'*64
            else:result['dest_identity']['st_ino']+=1
            result['crc32']=line_crc32(result)
            path.write_text(''.join(json.dumps(e,sort_keys=True,separators=(',',':'))+'\n' for e in events));before=self.artifacts()
            self.assertEqual(recover_execution(self.sandbox.settings,p['plan_id'])['phase'],'corrupted')
            with self.assertRaises(UndoRefused):undo_plan(self.sandbox.settings,p['plan_id'],confirm=True)
            self.assertEqual(self.artifacts(),before)

    def test_rehashed_invented_undo_result_is_rejected_before_repair_or_move(self):
        """撤销结果枚举和状态不能用合法 CRC 伪造完成。"""
        self.sandbox.add_file('group/report.pdf', b'UNDO PAYLOAD')
        p = self.plan(enabled=False, scope=('group',))
        original, immutable = self.frozen(p)
        self.assertEqual(self.execute(p).moved, 1)
        undo_journal = self.sandbox.journal(p['plan_id'], 'undo')
        op = p['operations'][0]
        undo_journal.append(make_event(
            'undo_intent', op['op_id'],
            **{'from': op['dest'], 'to': op['source'],
               'expected_identity': op['identity']},
        ))
        path = Path(undo_journal.generation_file(1))
        events = [json.loads(line) for line in path.read_text().splitlines()]
        result = {
            'event': 'undo_result', 'op_id': op['op_id'],
            'op_kind': 'file', 'log_kind': 'undo',
            'journal_schema_version': 2, 'plan_schema_version': 3,
            'plan_hash': p['plan_hash'],
            'outcome': 'invented', 'undo_state': 'restored',
            'reason_code': 'OK', 'reason': 'synthetic',
        }
        result['crc32'] = line_crc32(result)
        events.append(result)
        path.write_text(''.join(json.dumps(e, sort_keys=True, separators=(',', ':')) + '\n' for e in events))
        before = self.artifacts()
        source = Path(op['source']); dest = Path(op['dest'])
        self.assertFalse(source.exists()); self.assertTrue(dest.exists())
        recovery = recover_execution(self.sandbox.settings, p['plan_id'])
        self.assertEqual(recovery['phase'], 'corrupted')
        with self.assertRaises(UndoRefused):
            undo_plan(self.sandbox.settings, p['plan_id'], confirm=True)
        self.assertEqual(self.artifacts(), before)
        self.assertFalse(source.exists()); self.assertTrue(dest.exists())
        self.assertEqual(self.immutable(p), immutable)

    def test_valid_undo_result_and_interrupted_recovery_remain_accepted(self):
        """合法完成及 result 丢失后的公开恢复仍可正常还原。"""
        self.sandbox.add_file('complete/report.pdf', b'COMPLETE')
        complete = self.plan(scope=('complete',))
        original, immutable = self.frozen(complete)
        self.execute(complete)
        self.assertEqual(undo_plan(self.sandbox.settings, complete['plan_id'], confirm=True).restored, 1)
        self.assertEqual(self.immutable(complete), immutable)
        self.assertFileContent(original[0][0]['source'], original[0][1])
        self.assertNotExists(original[0][0]['dest'])

        self.sandbox.add_file('interrupted/report.pdf', b'INTERRUPTED')
        interrupted = self.plan(scope=('interrupted',))
        original2, immutable2 = self.frozen(interrupted)
        tripped = []
        def interrupt(event, op):
            if event == 'before_directory_result' and not tripped:
                tripped.append(op['op_id'])
                raise JournalWriteFailed('SIMULATED RESULT LOSS')
        self.execute(interrupted, hook=interrupt)
        self.assertEqual(recover_execution(self.sandbox.settings, interrupted['plan_id'])['unknown'], [])
        self.assertEqual(undo_plan(self.sandbox.settings, interrupted['plan_id'], confirm=True).restored, 1)
        self.assertEqual(self.immutable(interrupted), immutable2)
        self.assertFileContent(original2[0][0]['source'], original2[0][1])
        self.assertNotExists(original2[0][0]['dest'])

    def test_tail_repair_and_missing_manifest_after_directory_move_preserve_sealed_bytes(self):
        self.sandbox.add_file('group/report.pdf',b'TAIL');p=self.plan();original,immutable=self.frozen(p);self.execute(p)
        journal=self.sandbox.journal(p['plan_id']);path=Path(journal.generation_file(1));path.write_bytes(path.read_bytes()+b'{"event":"result"');before=path.read_bytes();Path(journal.manifest_path).unlink()
        report=recover_execution(self.sandbox.settings,p['plan_id']);self.assertEqual(report['unknown'],[]);self.assertEqual(path.read_bytes(),before)
        self.assertTrue(journal.classify().healthy);self.roundtrip(p,original,immutable,1)

    def test_enumeration_io_error_is_not_empty_and_does_not_move_directory(self):
        self.sandbox.add_file('group/report.pdf');p=self.plan();real=os.listdir;armed=[False]
        def hook(event,op):
            if event=='before_move':armed[0]=True
        def listdir(path):
            if armed[0] and isinstance(path,int):raise PermissionError(errno.EACCES,'SIMULATED ENUMERATION')
            return real(path)
        with patch('os.listdir',listdir):r=self.execute(p,hook)
        self.assertTrue(r.stopped);self.assertEqual(r.archived_dirs,0);self.assertExists(self.sandbox.downloads/'group')

    def test_same_source_two_batches_return_to_v0(self):
        session=OperationSession();plans=[]
        for rel in ('a/deep/report.pdf','b/deep/report.pdf'):
            self.sandbox.add_file(rel,rel.encode());p=self.plan(scope=(rel.split('/')[0],));original,immutable=self.frozen(p);plans.append((p,original,immutable));self.execute(p);session.track(self.sandbox.settings,p['plan_id'])
        r=session.return_to(0,undo_plan);self.assertTrue(r.reached,r);self.assertEqual((r.restored,r.restored_dirs),(2,4))
        for p,original,immutable in plans:
            self.assertEqual(self.immutable(p),immutable)
            for op,content in original:self.assertIdentityMatches(op['source'],op['identity']);self.assertFileContent(op['source'],content);self.assertNotExists(op['dest'])
            self.assertTrue(all(self.sandbox.journal(p['plan_id'],k).classify().healthy for k in ('execution','undo')))


class ArchiveGuiTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        from tests import test_gui
        self.root=None;self.app=None
        self.assertTrue(test_gui.DISPLAY_AVAILABLE,test_gui._DISPLAY_ERROR)

    def tearDown(self):
        if self.app is not None:self.app.on_close()
        elif self.root is not None:self.root.destroy()
        super().tearDown()

    def build(self):
        from tests import test_gui
        from doc_selecter import gui
        from tests.harness import require_gui_source
        from doc_selecter.config import ensure_state_dirs
        ensure_state_dirs(self.sandbox.settings)
        import tkinter as tk
        self.root=tk.Toplevel(test_gui._SHARED_ROOT);self.root.withdraw()
        self._real_dialogs={name:getattr(gui.messagebox,name) for name in ('showinfo','showwarning','showerror','askokcancel','askyesno')}
        self.calls=[]
        for name in self._real_dialogs:setattr(gui.messagebox,name,lambda t,m,_name=name,**k:(self.calls.append((_name,str(m))) or True))
        self.addCleanup(lambda:[setattr(gui.messagebox,n,f) for n,f in self._real_dialogs.items()])
        self.app=gui.App(self.root,self.sandbox.settings);require_gui_source(self.app,self.sandbox.settings.downloads_dir)
        return self.app

    def test_real_controls_archive_preview_confirm_execute_and_session_v0(self):
        self.sandbox.add_file('group/deep/report.pdf',b'GUI ARTIFICIAL');app=self.build()
        self.assertFalse(app.archive_var.get());app.archive_check.invoke()
        app.selected_scope=['group'];app.include_subfolders=True;app.subfolder_var.set(True);app.on_preview()
        self.assertTrue(app._preview_ready);self.assertIn('2',app.archive_label['text'])
        self.assertIn('dir-',app.directory_text.get('1.0','end'))
        p=app.plan;before=self.sandbox.plan_file(p['plan_id']).read_bytes();ident=p['operations'][0]['identity']
        app.confirm_button.invoke();app.execute_button.invoke()
        self.assertIn('暂停下载','\n'.join(m for n,m in self.calls if n=='askokcancel'))
        self.assertEqual(app.session.steps[-1].archived_dirs,2);self.assertNotExists(self.sandbox.downloads/'group')
        app.on_return_to(0);self.assertEqual(app.session.head,0)
        self.assertIdentityMatches(self.sandbox.downloads/'group/deep/report.pdf',ident);self.assertFileContent(self.sandbox.downloads/'group/deep/report.pdf',b'GUI ARTIFICIAL')
        for op in p['directory_operations']:self.assertEqual(list(dir_identity(op['source'])),[op['identity']['st_dev'],op['identity']['st_ino']]);self.assertNotExists(op['dest'])
        self.assertEqual(self.sandbox.plan_file(p['plan_id']).read_bytes(),before)
        self.assertFalse(any(n=='showerror' for n,m in self.calls),self.calls)

    def test_archive_switch_invalidates_confirmation_and_final_cancel_zero_moves(self):
        from doc_selecter import gui
        self.sandbox.add_file('group/report.pdf');app=self.build();app.on_preview();app.on_confirm();old=app.confirmed_hash
        self.assertIsNotNone(old);app.archive_check.invoke();self.assertIsNone(app.confirmed_hash);self.assertFalse(app._preview_ready)
        app.selected_scope=['group'];app.include_subfolders=True;app.subfolder_var.set(True);app.on_preview();app.on_confirm()
        p=app.plan;before={str(f):f.read_bytes() for f in self.sandbox.state.rglob('*') if f.is_file()}
        with patch.object(gui.messagebox,'askokcancel',return_value=False):app.execute_button.invoke()
        self.assertEqual({str(f):f.read_bytes() for f in self.sandbox.state.rglob('*') if f.is_file()},before)
        self.assertIdentityMatches(p['operations'][0]['source'],p['operations'][0]['identity']);self.assertExists(self.sandbox.downloads/'group')
