"""R11-A/B/C：公开准入、实际系统调用与真实 Tk 展示回归。"""
from __future__ import annotations

import copy
import io
import json
import os
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.harness import SandboxTestCase
from tests import test_gui as gui_tests
from doc_selecter import cli, gui
from doc_selecter.executor import ExecutionRefused, execute_plan
from doc_selecter.plan_hash import compute_plan_hash, verify_plan_hash
from doc_selecter.planner import (
    PlanError, assert_new_move_admissible, build_plan, confirmation_path,
    load_verified_plan, public_plan_view, write_confirmation, write_plan,
)
from doc_selecter.rules import redact_private_text


# 证据 runner 可保存实际调用记录；测试结果仍由强断言判定。
CALL_RECORDS = []


@contextmanager
def trace_paths():
    """用 open 的实际 dir_fd 链重建 stat 路径，追踪时不再查询文件系统。"""
    calls, fds = [], {}
    originals = {key: getattr(os, key) for key in ('open', 'close', 'stat', 'lstat', 'readlink')}
    realpath = os.path.realpath

    def path_text(path, kwargs):
        if isinstance(path, int):
            return fds.get(path, str(path))
        text = os.fsdecode(path)
        fd = kwargs.get('dir_fd')
        return os.path.join(fds[fd], text) if fd in fds else text

    def opened(path, flags, *args, **kwargs):
        text = path_text(path, kwargs)
        calls.append(dict(api='open', path=text, nofollow=bool(flags & os.O_NOFOLLOW)))
        fd = originals['open'](path, flags, *args, **kwargs)
        fds[fd] = text
        return fd

    def closed(fd):
        try:
            return originals['close'](fd)
        finally:
            fds.pop(fd, None)

    def wrapped(api):
        def call(path, *args, **kwargs):
            calls.append(dict(api=api, path=path_text(path, kwargs),
                              nofollow=api == 'lstat' or kwargs.get('follow_symlinks') is False))
            return originals[api](path, *args, **kwargs)
        return call

    def resolved(path, *args, **kwargs):
        calls.append(dict(api='realpath', path=os.fsdecode(path)))
        return realpath(path, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch('os.open', opened))
        stack.enter_context(patch('os.close', closed))
        for api in ('stat', 'lstat', 'readlink'):
            stack.enter_context(patch('os.' + api, wrapped(api)))
        stack.enter_context(patch('os.path.realpath', resolved))
        yield calls


class AdmissionRepairTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.source = self.sandbox.add_file('ordinary.pdf', b'ARTIFICIAL NORMAL')
        self.settings = self.sandbox.settings
        self.plan = build_plan(self.settings).plan

    def save(self, plan):
        plan['plan_hash'] = compute_plan_hash(plan, resolve_paths=False)
        write_plan(self.settings, plan)

    def public_rejections(self, plan, *, private_marker='medical-records', link=None):
        original = self.sandbox.plan_file(plan['plan_id']).read_bytes()
        confirmation = Path(confirmation_path(self.settings, plan['plan_id']))
        before = confirmation.read_bytes() if confirmation.exists() else None
        with trace_paths() as calls, patch('doc_selecter.executor.probe_capability') as probe, \
                patch('doc_selecter.planner.verify_plan_hash',
                      side_effect=AssertionError('refusal must precede fingerprint parsing')) as fingerprint:
            with self.assertRaises((PlanError, ExecutionRefused)):
                write_confirmation(self.settings, plan['plan_id'], plan['plan_hash'])
            with self.assertRaises((PlanError, ExecutionRefused)):
                execute_plan(self.settings, plan['plan_id'], plan['plan_hash'])
            for args in [('confirm', '--plan', plan['plan_id']),
                         ('execute', '--plan', plan['plan_id'], '--confirm', plan['plan_hash'])]:
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    self.assertEqual(cli.main(list(args)), 2)
                self.assertNotIn(private_marker, output.getvalue())
            probe.assert_not_called()
            fingerprint.assert_not_called()
        private = [c for c in calls if private_marker in c['path']]
        self.assertEqual(private, [], calls)
        self.assertEqual([c for c in calls if c['api'] == 'readlink'], [], calls)
        if link is not None:
            checks = [c for c in calls if c['path'] == str(link) and c['api'] == 'stat']
            self.assertTrue(checks, calls)
            self.assertTrue(all(c['nofollow'] for c in checks), checks)
        CALL_RECORDS.append(dict(test=self.id(), calls=calls, private_calls=private,
                                 confirmation_preserved=True, probe_calls=0))
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(), original)
        self.assertEqual(confirmation.read_bytes() if confirmation.exists() else None, before)
        self.assertEqual([p for p in Path(self.settings.journals_dir).rglob('*') if p.is_file()], [])

    def test_outside_source_sensitive_parent_refused_before_metadata(self):
        private = self.sandbox.root / 'medical-records' / 'ordinary.pdf'
        private.parent.mkdir(); private.write_bytes(b'ARTIFICIAL PRIVATE')
        self.plan['operations'][0]['source'] = str(private)
        self.save(self.plan)
        with patch('os.stat', side_effect=AssertionError('not pure')), \
                patch('os.lstat', side_effect=AssertionError('not pure')), \
                patch('os.path.realpath', side_effect=AssertionError('not pure')):
            with self.assertRaises(PlanError):
                assert_new_move_admissible(self.settings, self.plan)
        self.public_rejections(self.plan)
        self.assertIdentityMatches(self.source, self.plan['operations'][0]['identity'])
        self.assertFileContent(private, b'ARTIFICIAL PRIVATE')

    def test_private_declared_root_cannot_authorize_source(self):
        self.plan['downloads_dir'] = str(self.sandbox.root / 'medical-records')
        self.plan['operations'][0]['source'] = self.plan['downloads_dir'] + '/ordinary.pdf'
        self.save(self.plan); self.public_rejections(self.plan)

    def test_private_declared_target_root_cannot_authorize_dest(self):
        self.plan['target_root'] = str(self.sandbox.root / 'medical-records')
        self.plan['operations'][0]['dest'] = self.plan['target_root'] + '/ordinary.pdf'
        self.save(self.plan); self.public_rejections(self.plan)

    def test_actual_sensitive_parent_inside_root_cannot_hide_in_rel_path(self):
        private = self.sandbox.add_file('medical-records/ordinary.pdf')
        self.plan['operations'][0]['source'] = str(private)
        self.save(self.plan); self.public_rejections(self.plan)

    def test_outside_dest_sensitive_parent_refused_before_metadata(self):
        self.plan['operations'][0]['dest'] = str(self.sandbox.root / 'medical-records/ordinary.pdf')
        self.save(self.plan); self.public_rejections(self.plan)

    def test_expanded_scope_does_not_authorize_unselected_sibling(self):
        self.sandbox.add_file('A/deep/a.pdf'); sibling = self.sandbox.add_file('A/sibling/b.pdf')
        plan = build_plan(self.settings, recursive=True, scope_dirs=['A/deep']).plan
        op = plan['operations'][-1]
        op.update(rel_path='A/sibling/b.pdf', source=str(sibling),
                  dest=self.settings.dest_for(op['category'], 'b.pdf', 'A/sibling'))
        plan['scan_scope'].append('A/sibling')
        self.save(plan); self.public_rejections(plan)

    def test_nonrecursive_plan_cannot_authorize_nested_source(self):
        self.sandbox.add_file('A/deep/a.pdf')
        plan = build_plan(self.settings, recursive=True, scope_dirs=['A']).plan
        plan['recursive'] = False
        self.save(plan); self.public_rejections(plan)

    def test_traversal_source_refused_before_path_resolution(self):
        self.plan['operations'][0]['source'] = self.settings.canonical_downloads_dir + '/../medical-records/ordinary.pdf'
        # 非规范存储路径无法生成合法无元数据指纹；仍须先拒绝路径再解析指纹。
        write_plan(self.settings, self.plan); self.public_rejections(self.plan)

    def replaced_link(self, *, field='source', parent=False, dangling=False):
        self.save(self.plan)
        write_confirmation(self.settings, self.plan['plan_id'], self.plan['plan_hash'])
        private = self.sandbox.add_file('medical-records/inside.pdf')
        op = self.plan['operations'][0]
        if field == 'source' and parent:
            # 新计划先有普通父链，随后替换该链；不重设原身份。
            self.sandbox.add_file('Chosen/deep/file.pdf')
            self.plan = build_plan(self.settings, recursive=True, scope_dirs=['Chosen']).plan
            self.save(self.plan)
            write_confirmation(self.settings, self.plan['plan_id'], self.plan['plan_hash'])
            link = self.sandbox.downloads / 'Chosen'
            link.rename(self.sandbox.root / 'saved-parent')
            link.symlink_to(private.parent, target_is_directory=True)
        else:
            link = Path(op[field]).parent if parent else Path(op[field])
            if field == 'source':
                link.rename(self.sandbox.root / 'saved-source.pdf')
            else:
                link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(private.parent if parent else (private.with_name('missing.pdf') if dangling else private),
                            target_is_directory=parent)
        self.public_rejections(self.plan, link=link)
        self.assertTrue(link.is_symlink())
        # 只清理本测试植入的链接，人工原文件恢复后继续核对旧身份。
        link.unlink()
        if field == 'source':
            saved = self.sandbox.root / ('saved-parent' if parent else 'saved-source.pdf')
            saved.rename(link)
            for operation in self.plan['operations']:
                self.assertIdentityMatches(operation['source'], operation['identity'])

    def test_source_last_segment_link_refused_without_target_metadata(self):
        self.replaced_link()

    def test_source_dangling_link_refused_without_readlink(self):
        self.replaced_link(dangling=True)

    def test_source_parent_link_refused_without_target_metadata(self):
        self.replaced_link(parent=True)

    def test_dest_last_segment_link_refused_without_target_metadata(self):
        self.replaced_link(field='dest')

    def test_dest_parent_link_refused_without_target_metadata(self):
        self.replaced_link(field='dest', parent=True)

    def test_nofollow_open_refuses_parent_changed_after_stat(self):
        self.sandbox.add_file('Chosen/deep/a.pdf')
        self.plan = build_plan(self.settings, recursive=True, scope_dirs=['Chosen']).plan
        self.save(self.plan)
        private = self.sandbox.add_file('medical-records/inside.pdf')
        link = self.sandbox.downloads / 'Chosen'
        saved = self.sandbox.root / 'saved-race-parent'
        real_stat = os.stat
        changed = []
        def raced(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == 'Chosen' and kwargs.get('follow_symlinks') is False and not changed:
                link.rename(saved); link.symlink_to(private.parent, target_is_directory=True)
                changed.append(True)
            return info
        try:
            with patch('os.stat', raced):
                self.public_rejections(self.plan, link=link)
            self.assertEqual(changed, [True])
        finally:
            if changed:
                link.unlink(); saved.rename(link)
        for op in self.plan['operations']:
            self.assertIdentityMatches(op['source'], op['identity'])

    def test_current_canonical_hash_equals_original_hash_contract(self):
        self.assertEqual(compute_plan_hash(self.plan), compute_plan_hash(self.plan, resolve_paths=False))
        self.save(self.plan)
        self.assertTrue(verify_plan_hash(self.plan))
        write_confirmation(self.settings, self.plan['plan_id'], self.plan['plan_hash'])
        self.assertEqual(load_verified_plan(self.settings, self.plan['plan_id'], for_new_move=True), self.plan)

    def test_decomposed_unicode_path_admission_matches_historical_encoding(self):
        self.sandbox.add_file('Cafe\u0301/deep/说明.pdf')
        plan = build_plan(self.settings, recursive=True, scope_dirs=['Cafe\u0301/deep']).plan
        self.save(plan)
        write_confirmation(self.settings, plan['plan_id'], plan['plan_hash'])
        self.assertEqual(compute_plan_hash(plan), compute_plan_hash(plan, resolve_paths=False))
        self.assertEqual(load_verified_plan(self.settings, plan['plan_id'], for_new_move=True), plan)
        for op in plan['operations']:
            self.assertIdentityMatches(op['source'], op['identity'])
            self.assertNotExists(op['dest'])


class HistoricalRootGuiRepairTests(SandboxTestCase):
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

    def test_real_app_redacts_private_declared_root_without_metadata(self):
        self.sandbox.add_file('ordinary.pdf')
        settings = self.sandbox.settings
        plan = build_plan(settings).plan
        # Use a supported, frozen r11 v2 history shape.  This is display-only
        # material and must not be interpreted as a current v3 authorization.
        plan['schema_version'] = 2
        plan['rules_version'] = 'milestone4-privacy-r11'
        plan['plan_hash_alg'] = 'sha256/canonical-json-v1'
        plan.pop('archive', None)
        plan.pop('directory_operations', None)
        plan['downloads_dir'] = str(self.sandbox.downloads / '合同资料')
        plan['operations'][0]['source'] = plan['downloads_dir'] + '/ordinary.pdf'
        plan['plan_hash'] = compute_plan_hash(plan, resolve_paths=False)
        write_plan(settings, plan)
        original = self.sandbox.plan_file(plan['plan_id']).read_bytes()
        with trace_paths() as calls:
            self.assertEqual(load_verified_plan(settings, plan['plan_id'], display_only=True), plan)
            view = public_plan_view(plan, settings)
            self.app = self._build()
            # 切换来源不采信其他根的历史记录；单独保留真实 GUI 历史展示脱敏回归。
            self.assertIsNone(self.app.plan)
            self.app.plan = plan
            self.app._render_plan(plan)
            self.root.update_idletasks()
            rows = [list(self.app.tree.item(row, 'values')) for row in self.app.tree.get_children()]
        self.assertEqual([c for c in calls if '合同资料' in c['path']], [], calls)
        self.assertNotIn('合同资料', json.dumps(view, ensure_ascii=False))
        self.assertTrue(rows)
        self.assertNotIn('合同资料', json.dumps(rows, ensure_ascii=False))
        self.assertIn('疑似敏感信息已隐藏', json.dumps(rows, ensure_ascii=False))
        self.assertEqual(str(self.app.confirm_button['state']), 'disabled')
        self.assertEqual(str(self.app.execute_button['state']), 'disabled')
        self.assertEqual(self.app.plan, plan)
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(), original)
        CALL_RECORDS.append(dict(test=self.id(), calls=calls, rows=rows, private_calls=[],
                                 plan_preserved=True, real_tk_controls=True))

    def test_trusted_anchor_prefix_cannot_hide_sibling_private_name(self):
        root = '/trusted/medical'
        text = root + '-records/ordinary.pdf'
        self.assertNotIn('medical-records', redact_private_text(text, roots=(root,)))
        settings = self.sandbox.settings
        plan = dict(downloads_dir=str(self.sandbox.downloads / '合同资料'))
        original = copy.deepcopy(plan)
        with patch('os.stat', side_effect=AssertionError('display metadata')), \
                patch('os.lstat', side_effect=AssertionError('display metadata')), \
                patch('os.path.realpath', side_effect=AssertionError('display metadata')):
            self.assertNotIn('合同资料', str(public_plan_view(plan, settings)))
        self.assertEqual(plan, original)


if __name__ == '__main__':
    unittest.main()
