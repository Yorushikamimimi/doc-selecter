"""R11-D：macOS 名称中的字面反斜杠，同批公开入口和真实 GUI 闭环。"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tests.harness import SandboxTestCase
from tests import test_gui as gui_tests
from doc_selecter import gui
from doc_selecter.executor import ExecutionRefused, execute_plan
from doc_selecter.plan_hash import compute_plan_hash
from doc_selecter.planner import PlanError, build_plan, confirmation_path, write_confirmation, write_plan
from doc_selecter.snapshot import read_snapshot
from doc_selecter.undo import undo_plan


NAME = 'report' + chr(92) + '2026.pdf'
RESULTS = []


class BatchFixture(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.sandbox.add_file(NAME, b'ARTIFICIAL BACKSLASH DOCUMENT')
        self.sandbox.add_file('说明.pdf', b'ARTIFICIAL SECOND NORMAL FILE')
        self.settings = self.sandbox.settings

    def assert_batch_names(self, plan):
        self.assertEqual({op['rel_path'] for op in plan['operations']}, {NAME, '说明.pdf'})
        self.assertEqual(len(plan['operations']), 2)
        for op in plan['operations']:
            self.assertEqual(Path(op['source']).name, op['rel_path'])
            self.assertEqual(Path(op['dest']).name, op['rel_path'])

    def assert_moved(self, plan):
        for op in plan['operations']:
            self.assertIdentityMatches(op['dest'], op['identity'])
            self.assertNotExists(op['source'])

    def assert_restored(self, plan):
        for op in plan['operations']:
            self.assertIdentityMatches(op['source'], op['identity'])
            self.assertNotExists(op['dest'])
        snapshot = read_snapshot(self.settings, plan['plan_id'])
        self.assertEqual(len(snapshot['entries']), 2)
        self.assertTrue(all(e['undo_state'] == 'restored' for e in snapshot['entries']))

    def record(self, plan, **extra):
        RESULTS.append(dict(test=self.id(), names=[op['rel_path'] for op in plan['operations']],
                            identities=[op['identity'] for op in plan['operations']],
                            literal_backslash_preserved=NAME in [op['rel_path'] for op in plan['operations']],
                            **extra))


class BackslashBatchTests(BatchFixture):
    def test_public_api_confirm_move_undo_two_files_without_renaming(self):
        plan = build_plan(self.settings).plan
        self.assert_batch_names(plan)
        write_plan(self.settings, plan)
        original = self.sandbox.plan_file(plan['plan_id']).read_bytes()
        write_confirmation(self.settings, plan['plan_id'], plan['plan_hash'])
        confirmed = Path(confirmation_path(self.settings, plan['plan_id'])).read_bytes()
        result = execute_plan(self.settings, plan['plan_id'], plan['plan_hash'])
        self.assertEqual((result.moved, result.failed, result.skipped), (2, 0, 0))
        self.assert_moved(plan)
        undo = undo_plan(self.settings, plan['plan_id'], confirm=True)
        self.assertEqual(undo.restored, 2)
        self.assertEqual(undo.undo_status, 'completed')
        self.assert_restored(plan)
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(), original)
        self.assertEqual(Path(confirmation_path(self.settings, plan['plan_id'])).read_bytes(), confirmed)
        for kind, intent, completion in [('execution', 'intent', 'result'),
                                         ('undo', 'undo_intent', 'undo_result')]:
            events, _ = self.sandbox.journal(plan['plan_id'], kind).read_events()
            self.assertEqual(sum(e['event'] == intent and e.get('op_kind','file') == 'file' for e in events), 2)
            self.assertEqual(sum(e['event'] == completion and e.get('op_kind','file') == 'file' for e in events), 2)
        self.record(plan, moved=2, restored=2, five_fields_match=True, plan_and_confirmation_unchanged=True)

    def cli(self, *args):
        r = subprocess.run([sys.executable, '-m', 'doc_selecter', *args],
                           env=dict(os.environ, DOCSELECTER_TEST_ROOT=str(self.sandbox.root)),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return dict(command=list(args), exit=r.returncode, output=r.stdout + r.stderr)

    def test_public_cli_batch_confirm_execute_recover_undo(self):
        steps = [self.cli('plan')]
        plan = json.loads(next(Path(self.settings.plans_dir).glob('*.json')).read_text())
        self.assert_batch_names(plan)
        steps.append(self.cli('confirm', '--plan', plan['plan_id']))
        steps.append(self.cli('execute', '--plan', plan['plan_id'], '--confirm', plan['plan_hash']))
        self.assert_moved(plan)
        for args in [('status', '--plan', plan['plan_id']), ('recover', '--plan', plan['plan_id']),
                     ('undo', '--plan', plan['plan_id'], '--confirm'), ('recover', '--plan', plan['plan_id'])]:
            steps.append(self.cli(*args))
        self.assert_restored(plan)
        self.record(plan, steps=steps, moved=2, restored=2, five_fields_match=True)

    def test_slash_empty_dot_dotdot_and_nul_still_refused_before_fingerprint(self):
        plan = build_plan(self.settings).plan
        invalid = ['', '/ordinary.pdf', 'A//ordinary.pdf', './ordinary.pdf',
                   'A/../ordinary.pdf', 'ordinary.pdf/', 'ordinary' + chr(0) + '.pdf']
        for rel in invalid:
            with self.subTest(rel=repr(rel)):
                bad = copy.deepcopy(plan)
                bad['operations'][0]['rel_path'] = rel
                bad['plan_id']=plan['plan_id']+'-malformed-'+str(len(list(self.sandbox.plan_file(plan['plan_id']).parent.glob('*.json'))))
                write_plan(self.settings, bad)
                with patch('doc_selecter.planner.verify_plan_hash',
                           side_effect=AssertionError('must refuse before fingerprint')) as fingerprint, \
                        patch('doc_selecter.planner._reject_new_move_links',
                              side_effect=AssertionError('must refuse before metadata')) as paths, \
                        patch('doc_selecter.executor.probe_capability') as probe:
                    with self.assertRaises(PlanError):
                        write_confirmation(self.settings, bad['plan_id'], bad['plan_hash'])
                    with self.assertRaises((PlanError, ExecutionRefused)):
                        execute_plan(self.settings, bad['plan_id'], bad['plan_hash'])
                    fingerprint.assert_not_called(); paths.assert_not_called(); probe.assert_not_called()
                self.assertNotExists(confirmation_path(self.settings, bad['plan_id']))
        for op in plan['operations']:
            self.assertIdentityMatches(op['source'], op['identity'])

    def test_backslash_does_not_hide_sensitive_name(self):
        name = 'report' + chr(92) + '合同.pdf'
        private = self.sandbox.add_file(name, b'ARTIFICIAL PRIVATE')
        plan = build_plan(self.settings).plan
        self.assert_batch_names(plan)
        self.assertEqual(plan['counts']['sensitive_skipped'], 1)
        op = plan['operations'][0]
        op.update(rel_path=name, source=str(private),
                  dest=self.settings.dest_for(op['category'], name))
        plan['plan_hash'] = compute_plan_hash(plan, resolve_paths=False)
        write_plan(self.settings, plan)
        with patch('doc_selecter.planner.verify_plan_hash',
                   side_effect=AssertionError('must refuse before fingerprint')) as fingerprint, \
                patch('doc_selecter.planner._reject_new_move_links',
                      side_effect=AssertionError('must refuse before metadata')) as paths:
            with self.assertRaises(PlanError):
                write_confirmation(self.settings, plan['plan_id'], plan['plan_hash'])
            with self.assertRaises(ExecutionRefused):
                execute_plan(self.settings, plan['plan_id'], plan['plan_hash'])
            fingerprint.assert_not_called(); paths.assert_not_called()
        self.assertNotExists(confirmation_path(self.settings, plan['plan_id']))
        self.assertExists(private)


class BackslashGuiTests(BatchFixture):
    _stub_dialogs = gui_tests.GuiTests._stub_dialogs
    _restore_dialogs = gui_tests.GuiTests._restore_dialogs
    _build = gui_tests.GuiTests._build

    def setUp(self):
        super().setUp()
        self.assertTrue(gui_tests.DISPLAY_AVAILABLE, gui_tests._DISPLAY_ERROR)
        self.root = None
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.on_close()
        elif self.root is not None:
            self.root.destroy()
        super().tearDown()

    def test_real_gui_category_toggle_confirmation_execution_and_undo(self):
        self.app = self._build()
        self.app.preview_button.invoke()
        self.assert_batch_names(self.app.plan)
        self.assertEqual(str(self.app.confirm_button['state']), 'normal')
        first = self.app.plan['plan_id']
        # 操作真实类别 Checkbutton，走产品回调生成对应新计划。
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        checkbox = next(w for w in descendants(self.app.overview_frame)
                        if isinstance(w, gui.ttk.Checkbutton) and str(w['text']).startswith('压缩包'))
        checkbox.invoke()
        self.assertNotEqual(self.app.plan['plan_id'], first)
        self.assertNotIn('压缩包', self.app.plan['category_selection'])
        self.assert_batch_names(self.app.plan)
        rows = [list(self.app.tree.item(row, 'values')) for row in self.app.tree.get_children()]
        self.assertEqual({row[0] for row in rows}, {NAME, '说明.pdf'})
        self.assertEqual(str(self.app.confirm_button['state']), 'normal')
        self.app.confirm_button.invoke()
        self.assertEqual(self.app.confirmed_hash, self.app.plan['plan_hash'])
        self.assertEqual(str(self.app.execute_button['state']), 'normal')
        self.app.execute_button.invoke()
        self.assert_moved(self.app.plan)
        self.assertIn('成功 2', str(self.app.result_label['text']))
        self.app.on_undo()
        self.assert_restored(self.app.plan)
        self.assertFalse(any(kind == 'error' for kind, _ in self.dialog_calls), self.dialog_calls)
        self.record(self.app.plan, rows=rows, moved=2, restored=2, five_fields_match=True,
                    real_tk_controls=True, confirmation_enabled=True, dialogs=self.dialog_calls)
