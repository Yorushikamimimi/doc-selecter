"""R11：人工隐私样本、真实扫描调用、新移动准入与历史还原。"""
from __future__ import annotations

import copy
import json
import os
import io
from contextlib import redirect_stdout, redirect_stderr
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.harness import SandboxTestCase
from tests import test_gui as gui_tests
from doc_selecter import RULES_VERSION, gui
from doc_selecter.atomic_move import AtomicMover, lstat_identity
from doc_selecter.journal import make_event
from doc_selecter.executor import ExecutionRefused, execute_plan, recover_execution
from doc_selecter.plan_hash import compute_plan_hash, normalize_path, verify_plan_hash
from doc_selecter.planner import (
    PlanError, build_plan, capture_dir_identities, confirmation_path,
    public_plan_view, write_confirmation, write_plan,
)
from doc_selecter.rules import (
    SENSITIVE_TERMS, SENSITIVE_SUFFIXES, EXCLUDED_HIDDEN, EXCLUDED_DIR,
    EXCLUDED_SYMLINK, EXCLUDED_SENSITIVE, is_sensitive_name,
    is_sensitive_relative_path, redact_private_text,
)
from doc_selecter.scanner import (
    scan_scope, scan_top_level, list_selectable_folders, collect_files,
    collect_temp_files, _count_files,
)
from doc_selecter.snapshot import read_snapshot
from doc_selecter.undo import undo_plan, UndoRefused
from doc_selecter.watcher import NewFileWatcher


class PrivacyFixture(SandboxTestCase):
    def cli(self, *args):
        env = dict(os.environ, DOCSELECTER_TEST_ROOT=str(self.sandbox.root))
        result = subprocess.run([sys.executable, '-m', 'doc_selecter', *args],
                                capture_output=True, text=True, env=env, timeout=30)
        return result.returncode, result.stdout + result.stderr

    def historical_plan(self, rel='合同.pdf', *, version='milestone1-221', moved=False,
                        interrupted=False):
        """独立人工旧记录：不调用新移动准入、不伪称运行了旧产品。"""
        source = self.sandbox.add_file(rel, b'ARTIFICIAL HISTORY')
        plan = build_plan(self.sandbox.settings).plan
        parent, _, name = rel.rpartition('/')
        identity = lstat_identity(str(source)).as_dict()
        op = dict(op_id='op-0001', rel_path=rel, source=normalize_path(str(source)),
                  dest=normalize_path(self.sandbox.settings.dest_for('文档资料', name, parent)),
                  category='文档资料', classification_basis='扩展名 .pdf 命中「文档资料」规则',
                  identity=identity)
        plan.update(rules_version=version, operations=[op], recursive=bool(parent),
                    selected_scope=['', parent] if parent else [''],
                    scan_scope=['', parent] if parent else [''])
        if version != RULES_VERSION:
            plan.update(schema_version=2, rules_version='milestone4-privacy-r11', plan_hash_alg='sha256/canonical-json-v1')
            plan.pop('archive',None); plan.pop('directory_operations',None)
        plan['counts']['selected'] = 1
        plan['dir_identities'] = capture_dir_identities(self.sandbox.settings, [op])
        plan['plan_hash'] = compute_plan_hash(plan)
        self.assertTrue(verify_plan_hash(plan))
        write_plan(self.sandbox.settings, plan)
        # 模拟原版本已有确认记录，当前产品不得修改/迁移它。
        record = Path(confirmation_path(self.sandbox.settings, plan['plan_id']))
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(dict(schema_version=1, plan_id=plan['plan_id'],
                                         confirmed_plan_hash=plan['plan_hash'],
                                         confirmed_at='artificial-history')))
        if moved:
            journal = self.sandbox.journal(plan['plan_id'])
            journal.append(make_event('intent', op['op_id'], plan_hash=plan['plan_hash'],
                                      source=op['source'], dest=op['dest'],
                                      category=op['category'], identity=identity))
            Path(op['dest']).parent.mkdir(parents=True, exist_ok=True)
            AtomicMover().rename_excl(op['source'], op['dest'])
            self.assertIdentityMatches(op['dest'], identity)
            if not interrupted:
                journal.append(make_event('result', op['op_id'], outcome='moved',
                                          reason_code='OK', reason='已移动',
                                          dest_identity=identity))
        return plan, op

    def assert_rejected_intact(self, plan, op):
        old = self.sandbox.plan_file(plan['plan_id']).read_bytes()
        confirm = Path(confirmation_path(self.sandbox.settings, plan['plan_id']))
        confirmed = confirm.read_bytes()
        with patch('doc_selecter.executor.probe_capability') as probe:
            code, out = self.cli('confirm', '--plan', plan['plan_id'])
            self.assertEqual(code, 2, out)
            self.assertIn('重新生成预览', out)
            with self.assertRaises(ExecutionRefused):
                execute_plan(self.sandbox.settings, plan['plan_id'], plan['plan_hash'])
            probe.assert_not_called()
        code, out = self.cli('execute', '--plan', plan['plan_id'], '--confirm', plan['plan_hash'])
        self.assertEqual(code, 2, out)
        self.assertNotIn(op['rel_path'], out)
        self.assertIdentityMatches(op['source'], op['identity'])
        self.assertNotExists(self.sandbox.settings.target_root)
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(), old)
        self.assertEqual(confirm.read_bytes(), confirmed)


class PrivacyRuleTests(PrivacyFixture):
    def test_explicit_baseline_union_suffixes_and_case(self):
        for term in SENSITIVE_TERMS:
            with self.subTest(term=term):
                self.assertTrue(is_sensitive_name('sample-' + term.upper() + '.pdf'))
        for suffix in SENSITIVE_SUFFIXES:
            with self.subTest(suffix=suffix):
                self.assertTrue(is_sensitive_name('sample' + suffix.upper()))
        # 不只遍历实现常量：独立检查正式基线与旧额外保护的关键差异。
        for name in ['简历.pdf', '合同.pdf', '工资.pdf', 'PERSONAL.pdf', 'ID_DSA',
                     'sample.1password', 'sample.cer', 'sample.crt', 'sample.gpg', 'wallet.txt']:
            self.assertTrue(is_sensitive_name(name), name)

    def test_additional_patterns_and_neutral_controls(self):
        private = ['.env', '.env.local', '.env-production', 'a.env_backup',
                   'a-CV.pdf', 'a_ssn.txt', 'api_key.csv', 'a.access_key.json',
                   'AUTH.pdf', 'oauth.txt', 'a personal.pdf', 'sample@example.invalid.pdf',
                   'sample-00000000000.pdf', 'sample-000000000000.pdf',
                   'sample-00000000000000000X.pdf', 'sample-000000000000000000000.pdf']
        neutral = ['说明.pdf', 'report.pdf', 'cvtool.pdf', 'ssninfo.txt', 'author.pdf',
                   'mypersonalization.txt', 'release-20261002.pdf', 'part-1234567890.pdf',
                   '123456-123456.pdf', 'example@local', 'environment.md']
        for name in private:
            with self.subTest(private=name): self.assertTrue(is_sensitive_name(name))
        for name in neutral:
            with self.subTest(neutral=name): self.assertFalse(is_sensitive_name(name))
        self.assertFalse(is_sensitive_relative_path('A/资料/说明.pdf'))
        self.assertTrue(is_sensitive_relative_path('A/合同资料/说明.pdf'))
        root = '/private/tmp/artificial-sandbox/Downloads'
        for name in ['a.CER', 'a.pem', 'a.env', '.env', 'sample@example.invalid.pdf']:
            message = f"[Errno 13] Permission denied: '{root}/{name}'"
            self.assertNotIn(name, redact_private_text(message, roots=(root,)))
        neutral = f"[Errno 13] Permission denied: '{root}/说明.pdf'"
        self.assertEqual(redact_private_text(neutral, roots=(root,)), neutral)

    def test_config_cannot_claim_old_rule_version(self):
        self.sandbox.add_file('说明.pdf')
        settings = replace(self.sandbox.settings, rules_version='milestone1-221')
        self.assertEqual(build_plan(settings).plan['rules_version'], RULES_VERSION)

    def test_hidden_sensitive_suffix_precedes_hidden_exclusion(self):
        for name in ['.sample.PEM', '.sample.CER', '.sample.1PASSWORD']:
            self.assertTrue(is_sensitive_name(name))


class PrivacyScanTests(PrivacyFixture):
    def traced_scandir(self, visits, metadata):
        real = os.scandir
        class Entry:
            def __init__(self, entry): self._entry = entry; self.name = entry.name
            def __getattr__(self, key):
                value = getattr(self._entry, key)
                if key in ('stat', 'is_dir', 'is_file', 'is_symlink'):
                    def call(*args, **kwargs):
                        metadata.append((self.name, key))
                        return value(*args, **kwargs)
                    return call
                return value
        class Entries:
            def __init__(self, path):
                visits.append(os.fspath(path)); self.inner = real(path)
            def __enter__(self): return self
            def __exit__(self, *args): self.inner.close()
            def __iter__(self): return self
            def __next__(self): return Entry(next(self.inner))
        return Entries

    def seed_private(self):
        names = ['合同.pdf', '简历.pdf', 'sample.cer', 'private-files',
                 'bankstatement-collection', '密码备份']
        for name in names[:3]: self.sandbox.add_file(name)
        for name in names[3:]: self.sandbox.add_file(name + '/inside.pdf')
        self.sandbox.add_file('A/说明.pdf')
        self.sandbox.add_file('A/合同.pdf')
        self.sandbox.add_file('A/medical-items/inside.pdf')
        return names + ['medical-items']

    def test_sensitive_names_precede_real_metadata_in_all_scanners(self):
        names = self.seed_private()
        visits, metadata = [], []
        with patch('doc_selecter.scanner.os.scandir', self.traced_scandir(visits, metadata)):
            top = scan_top_level(str(self.sandbox.downloads), target_parent_name='下载分类')
            scan = scan_scope(str(self.sandbox.downloads), target_parent_name='下载分类',
                              scope_dirs=['A', 'private-files'], recursive=True)
            folders = list_selectable_folders(str(self.sandbox.downloads), target_parent_name='下载分类')
            self.assertEqual(_count_files(str(self.sandbox.downloads), max_depth=8), 1)
            self.assertEqual(set(collect_files(str(self.sandbox.downloads), target_parent_name='下载分类',
                                              scope_dirs=['A'], recursive=True)), {'A/说明.pdf'})
        self.assertEqual(top.sensitive_count, 6)
        self.assertEqual(scan.sensitive_count, 8)
        self.assertEqual([f.rel_path for f in folders], ['A'])
        for name in names + ['合同.pdf']:
            self.assertFalse(any(n == name for n, _ in metadata), (name, metadata))
            self.assertFalse(any(name in p.split(os.sep) for p in visits), (name, visits))
        self.assertEqual(scan.sensitive_names, [])
        self.assertNotIn('private-files', repr(scan))

    def test_changes_errors_scope_and_persisted_plan_redact_names(self):
        names = self.seed_private()
        bad = self.sandbox.add_file('A/ordinary.pdf')
        visits, metadata = [], []
        trace = self.traced_scandir(visits, metadata)
        real_scandir = os.scandir
        calls = 0
        def changing(path):
            nonlocal calls
            if os.fspath(path) == str(self.sandbox.downloads):
                calls += 1
                if calls == 3:
                    self.sandbox.add_file('passport-new.pdf')
                    (self.sandbox.downloads / '简历.pdf').unlink()
            return trace(path)
        with patch('doc_selecter.scanner.os.scandir', changing):
            scan = scan_scope(str(self.sandbox.downloads), target_parent_name='下载分类',
                              scope_dirs=['A', 'private-files'], recursive=True)
        # 单独注入非敏感目录的 I/O 错误，异常字符串故意含人工敏感名称。
        def failed(path):
            if os.fspath(path) == str(bad.parent):
                raise PermissionError('合同.pdf denied')
            return real_scandir(path)
        with patch('doc_selecter.scanner.os.scandir', failed):
            failed_scan = scan_scope(str(self.sandbox.downloads), target_parent_name='下载分类',
                                     scope_dirs=['A'], recursive=True)
        self.assertGreater(failed_scan.failure_count, 0)
        plan = build_plan(self.sandbox.settings, scan=scan).plan
        write_plan(self.sandbox.settings, plan)
        blob = self.sandbox.plan_file(plan['plan_id']).read_text() + repr(scan) + repr(failed_scan)
        for name in names + ['passport-new.pdf']:
            self.assertNotIn(name, blob)
        self.assertNotIn('合同.pdf', blob)
        self.assertEqual(scan.added_during_scan, [])
        self.assertEqual(scan.removed_during_scan, [])

    def test_metadata_failure_cannot_disclose_sensitive_filename(self):
        self.sandbox.add_file('合同.pdf')
        self.sandbox.add_file('ordinary.pdf')
        real_scandir = os.scandir
        class Entry:
            def __init__(self, e): self.e = e; self.name = e.name
            def stat(self, **kwargs):
                if self.name == '合同.pdf': raise AssertionError('sensitive stat called')
                raise OSError('sample-简历.pdf')
        class Entries:
            def __init__(self, p): self.inner = real_scandir(p)
            def __enter__(self): return self
            def __exit__(self, *a): self.inner.close()
            def __iter__(self): return self
            def __next__(self): return Entry(next(self.inner))
        with patch('doc_selecter.scanner.os.scandir', Entries):
            scan = scan_top_level(str(self.sandbox.downloads), target_parent_name='下载分类')
        self.assertEqual(scan.metadata_failure_counts, {'OSError': 1})
        self.assertEqual(scan.sensitive_count, 1)
        self.assertNotIn('合同.pdf', repr(scan))
        self.assertNotIn('sample-简历.pdf', repr(scan))

    def test_watcher_and_temp_paths_use_same_privacy_policy(self):
        self.seed_private()
        self.sandbox.add_file('A/payroll.tmp')
        self.sandbox.add_file('A/ordinary.tmp')
        times = [0.0]
        watcher = NewFileWatcher(clock=lambda: times[0])
        watcher.enable()
        first = watcher.check_once(downloads_dir=str(self.sandbox.downloads), target_parent_name='下载分类', scope_dirs=['A'], recursive=True)
        times[0] = 6.0
        second = watcher.check_once(downloads_dir=str(self.sandbox.downloads), target_parent_name='下载分类', scope_dirs=['A'], recursive=True)
        self.assertEqual(second.new_files, ['A/说明.pdf'])
        self.assertEqual(collect_temp_files(str(self.sandbox.downloads), target_parent_name='下载分类',
                                           scope_dirs=['A'], recursive=True), ['A/ordinary.tmp'])
        for report in (first, second):
            text = watcher.format_report(report) + repr(report)
            for name in ('合同.pdf', 'private-files', 'medical-items', 'payroll.tmp'):
                self.assertNotIn(name, text)

    def test_top_hidden_folder_is_mutually_exclusive_and_not_entered(self):
        self.sandbox.add_file('.hidden/inside.pdf')
        self.sandbox.add_file('.private/inside.pdf')
        self.sandbox.add_file('normal/inside.pdf')
        self.sandbox.add_symlink('.linked', str(self.sandbox.downloads / 'normal'))
        visits, metadata = [], []
        with patch('doc_selecter.scanner.os.scandir', self.traced_scandir(visits, metadata)):
            scan = scan_top_level(str(self.sandbox.downloads), target_parent_name='下载分类')
        counts = scan.excluded_primary_counts
        self.assertEqual(counts[EXCLUDED_HIDDEN], 1)
        self.assertEqual(counts[EXCLUDED_DIR], 1)
        self.assertEqual(counts[EXCLUDED_SYMLINK], 1)
        self.assertEqual(counts[EXCLUDED_SENSITIVE], 1)
        self.assertEqual(scan.excluded_count, 4)
        self.assertEqual(set(visits), {str(self.sandbox.downloads)})
        self.assertFalse(any(n == '.private' for n, _ in metadata))


class PrivacyAdmissionTests(PrivacyFixture):
    def test_old_version_even_neutral_plan_rejected(self):
        plan, op = self.historical_plan('ordinary.pdf')
        # 普通名字也要拒绝旧规则；输出可正常包含其名，不要求遮蔽。
        self.assert_rejected_intact(plan, op)

    def test_old_sensitive_plan_rejected_cli_and_direct_without_probe(self):
        plan, op = self.historical_plan()
        self.assert_rejected_intact(plan, op)

    def test_current_valid_hash_sensitive_file_still_rejected(self):
        plan, op = self.historical_plan(version=RULES_VERSION)
        self.assert_rejected_intact(plan, op)

    def test_current_valid_hash_sensitive_parent_still_rejected(self):
        plan, op = self.historical_plan('A/合同资料/ordinary.pdf', version=RULES_VERSION)
        self.assert_rejected_intact(plan, op)

    def test_sensitive_parent_rejected_before_binding_metadata(self):
        plan, _ = self.historical_plan('A/medical/ordinary.pdf', version=RULES_VERSION)
        real_lstat = os.lstat
        metadata = []
        def traced(path, *args, **kwargs):
            text = os.fspath(path)
            metadata.append(text)
            self.assertNotIn('/medical', text, '名称准入必须先于指纹 realpath 的 lstat')
            return real_lstat(path, *args, **kwargs)
        with patch('doc_selecter.executor.verify_plan_binding') as binding, \
             patch('doc_selecter.executor.probe_capability') as probe, patch('os.lstat', traced):
            from doc_selecter.cli import main
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(main(['confirm', '--plan', plan['plan_id']]), 2)
            self.assertIn('重新生成预览', output.getvalue())
            with self.assertRaises(ExecutionRefused):
                execute_plan(self.sandbox.settings, plan['plan_id'], plan['plan_hash'])
            binding.assert_not_called(); probe.assert_not_called()
        self.assertFalse(any('/medical' in p for p in metadata))

    def test_confirmation_writer_rejects_without_overwriting_old_record(self):
        plan, _ = self.historical_plan()
        p = Path(confirmation_path(self.sandbox.settings, plan['plan_id']))
        before = p.read_bytes()
        with self.assertRaises(PlanError):
            write_confirmation(self.sandbox.settings, plan['plan_id'], plan['plan_hash'])
        self.assertEqual(p.read_bytes(), before)

    def test_current_hash_neutral_relpath_cannot_hide_sensitive_actual_paths(self):
        plan, op = self.historical_plan(version=RULES_VERSION)
        plan['operations'][0]['rel_path'] = 'ordinary.pdf'
        plan['plan_hash'] = compute_plan_hash(plan)
        # 人工伪造副本只用于反例；不覆盖既有不可变记录。
        plan['plan_id'] += '-forged'
        write_plan(self.sandbox.settings, plan)
        with self.assertRaises(ExecutionRefused) as ctx:
            execute_plan(self.sandbox.settings, plan['plan_id'], plan['plan_hash'])
        self.assertIn('重新生成预览', str(ctx.exception))
        self.assertIdentityMatches(op['source'], op['identity'])

    def test_stale_scan_cannot_publish_sensitive_plan_or_capture_parent_metadata(self):
        self.sandbox.add_file('ordinary.pdf')
        scan = scan_top_level(str(self.sandbox.downloads), target_parent_name='下载分类')
        scan.included[0] = replace(scan.included[0], rel_path='合同.pdf', name='合同.pdf')
        with patch('doc_selecter.planner.capture_dir_identities') as identities:
            with self.assertRaises(PlanError): build_plan(self.sandbox.settings, scan=scan)
            identities.assert_not_called()

    def test_neutral_public_cli_roundtrip_and_recover(self):
        self.sandbox.add_file('top.pdf')
        self.sandbox.add_file('A/deep/说明.pdf')
        self.sandbox.add_file('A/sibling/keep.pdf')
        code, out = self.cli('plan', '--include-subfolder', 'A/deep')
        self.assertEqual(code, 0, out)
        path = next((self.sandbox.state / 'plans').glob('*.json'))
        plan = json.loads(path.read_text())
        self.assertEqual([op['rel_path'] for op in plan['operations']], ['A/deep/说明.pdf', 'top.pdf'])
        for args in [('confirm', '--plan', plan['plan_id']),
                     ('execute', '--plan', plan['plan_id'], '--confirm', plan['plan_hash']),
                     ('recover', '--plan', plan['plan_id']),
                     ('undo', '--plan', plan['plan_id'], '--confirm'),
                     ('recover', '--plan', plan['plan_id'])]:
            code, out = self.cli(*args); self.assertEqual(code, 0, out)
        for op in plan['operations']:
            self.assertIdentityMatches(op['source'], op['identity']); self.assertNotExists(op['dest'])
        self.assertExists(self.sandbox.downloads / 'A/sibling/keep.pdf')


class PrivacyHistoryTests(PrivacyFixture):
    def test_old_successful_sensitive_history_public_undo_recover_preserves_records(self):
        plan, op = self.historical_plan('A/合同资料/合同.pdf', moved=True)
        original = self.sandbox.plan_file(plan['plan_id']).read_bytes()
        confirmation = Path(confirmation_path(self.sandbox.settings, plan['plan_id'])).read_bytes()
        journal = Path(self.sandbox.journal(plan['plan_id']).generation_file(1))
        before = journal.read_bytes()
        for command in [('recover', '--plan', plan['plan_id']),
                        ('status', '--plan', plan['plan_id']),
                        ('undo', '--plan', plan['plan_id'], '--confirm'),
                        ('recover', '--plan', plan['plan_id'])]:
            code, text = self.cli(*command)
            self.assertEqual(code, 0, text)
            self.assertNotIn('合同', text)
            self.assertNotIn('corrupted', text)
        self.assertIdentityMatches(op['source'], op['identity']); self.assertNotExists(op['dest'])
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(), original)
        self.assertEqual(Path(confirmation_path(self.sandbox.settings, plan['plan_id'])).read_bytes(), confirmation)
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(read_snapshot(self.sandbox.settings, plan['plan_id'])['entries'][0]['undo_state'], 'restored')

    def test_old_interrupted_sensitive_history_public_recover_then_undo(self):
        plan, op = self.historical_plan(moved=True, interrupted=True)
        journal = Path(self.sandbox.journal(plan['plan_id']).generation_file(1))
        original = journal.read_bytes()
        code, text = self.cli('recover', '--plan', plan['plan_id'])
        self.assertEqual(code, 0, text); self.assertNotIn('合同.pdf', text)
        self.assertNotIn('corrupted', text)
        self.assertTrue(journal.read_bytes().startswith(original))
        snap = read_snapshot(self.sandbox.settings, plan['plan_id'])
        self.assertEqual(snap['entries'][0]['undo_state'], 'pending')
        self.assertEqual(snap['entries'][0]['dest_identity'], op['identity'])
        code, text = self.cli('undo', '--plan', plan['plan_id'], '--confirm')
        self.assertEqual(code, 0, text); self.assertIdentityMatches(op['source'], op['identity'])

    def test_old_history_changed_identity_remains_blocked(self):
        plan, op = self.historical_plan(moved=True)
        Path(op['dest']).write_bytes(b'CHANGED ARTIFICIAL FILE')
        result = undo_plan(self.sandbox.settings, plan['plan_id'], confirm=True)
        self.assertEqual(result.restored, 0)
        self.assertEqual(result.entries[0]['undo_state'], 'blocked_dest_changed')
        self.assertEqual(result.entries[0]['dest_identity'], op['identity'])
        self.assertFileContent(op['dest'], b'CHANGED ARTIFICIAL FILE')
        self.assertNotExists(op['source'])

    def test_old_history_binding_error_cli_redacts_sensitive_parent(self):
        plan, op = self.historical_plan('A/medical-items/合同.pdf', moved=True)
        parent = Path(op['source']).parent
        parent.rename(parent.with_name('saved-history-dir'))
        parent.mkdir()
        code, text = self.cli('undo', '--plan', plan['plan_id'], '--confirm')
        self.assertEqual(code, 2, text)
        self.assertNotIn('medical-items', text); self.assertNotIn('合同.pdf', text)
        self.assertIdentityMatches(op['dest'], op['identity'])

    def test_old_history_corrupt_log_preserved_and_refused(self):
        plan, op = self.historical_plan(moved=True)
        journal = Path(self.sandbox.journal(plan['plan_id']).generation_file(1))
        journal.write_bytes(journal.read_bytes().replace(b'"moved"', b'"bogus"'))
        before = journal.read_bytes()
        with self.assertRaises((UndoRefused, ExecutionRefused)):
            undo_plan(self.sandbox.settings, plan['plan_id'], confirm=True)
        report = recover_execution(self.sandbox.settings, plan['plan_id'])
        self.assertEqual(report['phase'], 'corrupted')
        self.assertEqual(journal.read_bytes(), before)
        self.assertIdentityMatches(op['dest'], op['identity'])
        self.assertNotExists(op['source'])


class PrivacyGuiTests(PrivacyFixture):
    _stub_dialogs = gui_tests.GuiTests._stub_dialogs
    _restore_dialogs = gui_tests.GuiTests._restore_dialogs
    _build = gui_tests.GuiTests._build

    def setUp(self):
        super().setUp()
        if not gui_tests.DISPLAY_AVAILABLE: self.skipTest(gui_tests._DISPLAY_ERROR)
        self.root = None
        self.app = None

    def tearDown(self):
        if self.app is not None: self.app.on_close()
        elif self.root is not None: self.root.destroy()
        super().tearDown()

    def visible(self):
        texts = []
        def walk(w):
            if 'text' in w.keys(): texts.append(str(w['text']))
            for child in w.winfo_children(): walk(child)
        walk(self.root)
        texts.append(self.app.skip_text.get('1.0', 'end'))
        texts.extend(str(self.app.tree.item(row, 'values')) for row in self.app.tree.get_children())
        texts.extend(m for _, m in self.dialog_calls)
        return '\n'.join(texts)

    def test_historical_sensitive_preview_redacted_cannot_confirm_or_execute(self):
        plan, op = self.historical_plan('A/合同资料/合同.pdf')
        original = copy.deepcopy(plan)
        real_lstat = os.lstat
        def history_display_stat(path, *args, **kwargs):
            text = os.fspath(path)
            self.assertNotIn('/合同资料', text)
            self.assertNotIn('/合同.pdf', text)
            return real_lstat(path, *args, **kwargs)
        with patch('os.lstat', history_display_stat):
            self.app = self._build()
        self.assertNotIn('合同', self.visible())
        self.assertIn('历史方案', self.visible())
        self.assertEqual(str(self.app.confirm_button['state']), 'disabled')
        self.assertEqual(str(self.app.execute_button['state']), 'disabled')
        p = Path(confirmation_path(self.sandbox.settings, plan['plan_id']))
        before = p.read_bytes()
        with patch('doc_selecter.executor.probe_capability') as probe:
            self.app.on_confirm(); self.app.on_execute(); self.app.perform_execution()
            probe.assert_not_called()
        self.assertEqual(p.read_bytes(), before)
        self.assertIdentityMatches(op['source'], op['identity'])
        self.assertEqual(self.app.plan, original)
        self.assertNotIn('合同', self.visible())

    def test_forged_current_sensitive_plan_gui_refuses(self):
        plan, op = self.historical_plan(version=RULES_VERSION)
        self.app = self._build()
        self.app._preview_ready = True  # 即使调用方误置此状态，当前名称准入仍须生效。
        self.app.confirmed_hash = plan['plan_hash']
        self.app.on_confirm()
        self.assertIsNone(self.app.confirmed_hash)
        self.assertIsNone(self.app.perform_execution())
        self.assertIdentityMatches(op['source'], op['identity'])
        self.assertNotIn('合同.pdf', self.visible())

    def test_old_neutral_preview_is_history_until_new_successful_preview(self):
        plan, op = self.historical_plan('ordinary.pdf')
        self.app = self._build()
        self.app.on_confirm(); self.assertFalse(self.app._preview_ready)
        self.assertIsNone(self.app.perform_execution())
        self.app.on_preview(); self.assertNotEqual(self.app.plan['plan_id'], plan['plan_id'])
        self.app.on_confirm(); self.assertEqual(self.app.perform_execution().moved, 1)
        self.app.on_undo(); self.assertIdentityMatches(op['source'], op['identity'])

    def test_actual_app_async_watcher_uses_new_privacy_rules(self):
        self.sandbox.add_file('ordinary.pdf')
        self.app = self._build(); self.app.on_preview()
        self.sandbox.add_file('合同.pdf')
        self.sandbox.add_file('medical-items/inside.pdf')
        self.sandbox.add_file('new.pdf')
        now = [0.0]
        self.app.watcher.set_clock(lambda: now[0]); self.app.watcher.enable()
        old_plan = copy.deepcopy(self.app.plan)
        for t in [0.0, 6.0]:
            now[0] = t
            self.app.on_check_now()
            thread = self.app._discovery_thread
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.app.root.after_cancel(self.app._discovery_poll_job)
            self.app._poll_discovery()
        text = self.app.watch_text.get('1.0', 'end')
        self.assertIn('new.pdf', text)
        self.assertNotIn('合同.pdf', text); self.assertNotIn('medical-items', text)
        self.assertEqual(self.app.plan, old_plan)
        self.assertExists(self.sandbox.downloads / 'new.pdf')
        self.assertIsNone(self.app.confirmed_hash)

    def test_sensitive_history_gui_recover_undo_retains_capability(self):
        plan, op = self.historical_plan('A/合同资料/合同.pdf', moved=True)
        recover_execution(self.sandbox.settings, plan['plan_id'])
        self.app = self._build()
        self.assertEqual(str(self.app.undo_button['state']), 'normal')
        self.assertNotIn('合同', self.visible())
        self.app.on_recover(); self.app.on_undo()
        self.assertIdentityMatches(op['source'], op['identity'])
        self.assertNotExists(op['dest'])
        self.assertNotIn('合同', self.visible())

    def test_gui_historical_binding_error_is_caught_and_redacted(self):
        plan, op = self.historical_plan('A/medical-items/合同.pdf', moved=True)
        self.app = self._build()
        parent = Path(op['source']).parent
        parent.rename(parent.with_name('saved-dir'))
        parent.mkdir()
        self.app.on_recover()
        self.assertTrue(any(kind == 'error' for kind, _ in self.dialog_calls))
        self.assertNotIn('medical-items', self.visible()); self.assertNotIn('合同.pdf', self.visible())
        self.assertIdentityMatches(op['dest'], op['identity'])

    def test_new_gui_preview_watcher_and_error_do_not_disclose_private_samples(self):
        self.sandbox.add_file('合同.pdf')
        self.sandbox.add_file('说明.pdf')
        self.app = self._build(); self.app.on_preview()
        self.assertNotIn('合同.pdf', self.visible())
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.app.on_confirm(); self.assertEqual(self.app.perform_execution().moved, 1)
        self.app.on_undo()
        op = self.app.plan['operations'][0]
        self.assertIdentityMatches(op['source'], op['identity'])
        with patch('doc_selecter.gui.build_plan', side_effect=OSError('合同.pdf denied')):
            self.app.on_preview()
        self.assertNotIn('合同.pdf', self.visible())
        self.assertFalse(self.app._preview_ready)

    def test_history_projection_does_not_mutate_original_or_system_prefix(self):
        plan, _ = self.historical_plan()
        original = copy.deepcopy(plan)
        self.assertTrue(verify_plan_hash(plan, resolve_paths=False))
        changed = copy.deepcopy(plan)
        changed['operations'][0]['identity']['st_ino'] += 1
        self.assertFalse(verify_plan_hash(changed, resolve_paths=False))
        view = public_plan_view(plan, self.sandbox.settings)
        self.assertNotIn('合同.pdf', json.dumps(view, ensure_ascii=False))
        self.assertEqual(plan, original)
        plan['plan_hash'] = 'sha256:' + '0' * 64
        self.assertEqual(public_plan_view(plan, self.sandbox.settings)['plan_hash'], plan['plan_hash'])
        self.sandbox.add_file('normal.pdf')
        current = build_plan(self.sandbox.settings).plan
        view = public_plan_view(current, self.sandbox.settings)
        self.assertIn('normal.pdf', repr(view))
        self.assertEqual(view['operations'][0]['source'], current['operations'][0]['source'])
