"""Portable policy/API-boundary tests; these do NOT prove Windows operation."""
import ctypes
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from doc_selecter.windows.paths import absolute, component, under, unsupported_name, PLATFORM_ID
from doc_selecter.windows.policy import check_plan
from doc_selecter.atomic_move import SourceChangedError, AtomicMoveUnavailable, MoveFailed
from doc_selecter.windows import native, move
from doc_selecter.planner import PlanError
from doc_selecter.plan_hash import compute_plan_hash

REPO = Path(__file__).resolve().parents[1]


class WindowsPathPolicyTests(unittest.TestCase):
    def test_reserved_names_and_streams_rejected(self):
        for name in ('CON', 'nul.txt', 'COM1.zip', 'LPT9', 'com¹.doc', 'CONIN$', 'x:y', 'x.', 'x ',
                     '..', '.', '', 'a/b', 'a\\b', 'a\x00b', 'CON .txt'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                component(name)

    def test_unicode_not_silently_normalized(self):
        self.assertEqual(component('课程资料.pdf'), '课程资料.pdf')
        with self.assertRaises(ValueError):
            component('e\u0301.pdf')

    def test_raw_dot_segments_devices_network_relative_rejected(self):
        for value in ('C:notes', '/tmp', r'\\server\share', r'\\?\C:\notes', 'C:/a/../b', 'C:/a/./b', 'C:/a//b',
                      'C:/Users/Example/OneDrive/files', 'C:/Dropbox/files'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                absolute(value)

    def test_long_paths_rejected_without_truncation(self):
        with self.assertRaises(ValueError):
            absolute('C:/' + 'a' * 245)
        with self.assertRaises(ValueError):
            component('字' * 256)

    def test_drive_and_separator_canonicalization(self):
        self.assertEqual(absolute(r'c:\人工 资料\文件.pdf'), 'C:/人工 资料/文件.pdf')
        self.assertEqual(absolute('C:/'), 'C:/')

    def test_protected_root_case_and_sibling_boundary(self):
        self.assertTrue(under('C:/State/item', 'c:/state'))
        self.assertFalse(under('C:/State-other/item', 'C:/state'))

    def test_shortcuts_never_resolved(self):
        for value in ('folder.LNK', 'site.url', 'CON.txt'):
            self.assertTrue(unsupported_name(value))
        self.assertFalse(unsupported_name('课程.pdf'))

    def minimal_plan(self):
        return {'platform': PLATFORM_ID, 'schema_version': 3,
                'downloads_dir': 'C:/fake', 'target_root': 'C:/fake/下载分类',
                'archive': {'enabled': False}, 'directory_operations': [], 'operations': []}

    def test_foreign_plans_rejected(self):
        plan = self.minimal_plan(); plan.pop('platform')
        with self.assertRaises(PlanError):
            check_plan(plan)

    def test_archive_forgery_rejected(self):
        for change in ({'schema_version': 4}, {'archive': {'enabled': True}}, {'directory_operations': [{}]}):
            plan = self.minimal_plan(); plan.update(change)
            with self.assertRaises(PlanError):
                check_plan(plan)

    def test_plan_platform_is_hash_bound(self):
        plan = json.loads((REPO / 'tests/fixtures/step1_v2_plan.json').read_text(encoding='utf-8').replace(
            '<SOURCE>', 'C:/artificial' if os.name == 'nt' else '/artificial'))
        # Use the stored projection; no metadata queries against fixture paths.
        before = compute_plan_hash(plan, resolve_paths=False)
        plan['platform'] = PLATFORM_ID
        self.assertNotEqual(compute_plan_hash(plan, resolve_paths=False), before)
        plan.pop('platform')
        self.assertEqual(compute_plan_hash(plan, resolve_paths=False), before)


class WindowsMoveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.st = SimpleNamespace(st_dev=7, st_ino=2 ** 100 + 5, st_size=10,
                                  st_mtime_ns=100, st_mode=0o100600)
        self.expected = move.identity(self.st).as_dict()
        self.src, self.dst = SimpleNamespace(handle=1), SimpleNamespace(handle=2)

    def test_requires_confirmation_identity(self):
        with patch.object(native, 'api', return_value=(None, None)):
            mover = move.WindowsAtomicMover()
        with self.assertRaises(AtomicMoveUnavailable):
            mover.rename_excl_at(self.src, 'a', self.dst, 'b')

    def test_changed_source_never_calls_rename(self):
        with patch.object(native, 'api'), patch.object(native, 'info', return_value=self.st), \
             patch.object(native, 'nt_open', return_value=3), patch.object(native, 'close'), \
             patch.object(native, 'rename') as rename:
            expected = {**self.expected, 'st_ino': 999}
            with self.assertRaises(SourceChangedError):
                move.WindowsAtomicMover().rename_excl_at(self.src, 'a', self.dst, 'b', expected=expected)
            rename.assert_not_called()

    def test_native_rename_receives_source_and_destination_handles(self):
        with patch.object(native, 'api'), patch.object(native, 'info', return_value=self.st), \
             patch.object(native, 'nt_open', return_value=3) as opened, patch.object(native, 'close'), \
             patch.object(native, 'rename') as rename:
            move.WindowsAtomicMover().rename_excl_at(self.src, 'a', self.dst, 'b', expected=self.expected)
            rename.assert_called_once_with(3, self.dst, 'b')
            self.assertEqual(opened.call_args.kwargs['share'], 1)
            self.assertTrue(opened.call_args.kwargs['access'] & native.DELETE)

    def test_cross_volume_refused_before_source_open(self):
        with patch.object(native, 'api'), patch.object(native, 'info', side_effect=[self.st, SimpleNamespace(st_dev=8)]), \
             patch.object(native, 'nt_open') as opened, patch.object(native, 'close'):
            with self.assertRaises(MoveFailed):
                move.WindowsAtomicMover().rename_excl_at(self.src, 'a', self.dst, 'b', expected=self.expected)
            opened.assert_not_called()

    def test_post_move_failure_is_not_reported_as_safe_skip(self):
        with patch.object(native, 'api'), patch.object(native, 'info', side_effect=[self.st, self.st, self.st, OSError('after')]), \
             patch.object(native, 'nt_open', return_value=3), patch.object(native, 'close'), patch.object(native, 'rename'):
            with self.assertRaises(RuntimeError):
                move.WindowsAtomicMover().rename_excl_at(self.src, 'a', self.dst, 'b', expected=self.expected)

    def test_x64_struct_layout(self):
        self.assertEqual(ctypes.sizeof(native.ID_INFO), 24)
        self.assertEqual(native.RENAME.FileName.offset, 20)
        self.assertEqual(ctypes.sizeof(native.OBJECT_ATTRIBUTES), 48)

    def test_directory_flush_never_silently_succeeds(self):
        from doc_selecter.windows.io import os as windows_os
        with self.assertRaises(OSError):
            windows_os.fsync(native.Directory('C:/fake', []))

    def test_repeated_close_cannot_release_a_reused_descriptor(self):
        from doc_selecter.windows import io
        from unittest.mock import Mock
        stream = Mock()
        wrapper = io.File(stream, 91)
        with patch.object(io, '_release') as release:
            wrapper.close()
            wrapper.close()  # The OS may have reassigned fd 91 in between.
            release.assert_called_once_with(91)
        stream.close.assert_called_once_with()

    def test_owner_rights_requires_a_trusted_actual_owner(self):
        allowed = {'CURRENT_USER', 'S-1-5-18', 'S-1-5-32-544'}
        self.assertTrue(native._allowed_writer('S-1-3-4', 'CURRENT_USER', allowed))
        self.assertFalse(native._allowed_writer('S-1-3-4', 'OTHER_USER', allowed))
        self.assertFalse(native._allowed_writer('S-1-3-4', None, allowed))

    def test_broad_writers_are_not_granted_by_trusted_ownership(self):
        allowed = {'CURRENT_USER', 'S-1-5-18', 'S-1-5-32-544'}
        for sid in ('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545', 'OTHER_USER'):
            self.assertFalse(native._allowed_writer(sid, 'CURRENT_USER', allowed))


class WindowsFallbackTests(unittest.TestCase):
    def test_missing_runtime_can_choose_tk_without_installing(self):
        from doc_selecter.windows import app
        with patch.object(app, 'webview_available', return_value=False), patch.object(app, 'notice', return_value=6):
            self.assertFalse(app.choose_webview())

    def test_declining_fallback_exits_without_launch(self):
        from doc_selecter.windows import app
        with patch.object(app, 'webview_available', return_value=False), patch.object(app, 'notice', return_value=7):
            with self.assertRaises(SystemExit):
                app.choose_webview()

    def test_present_runtime_needs_no_prompt(self):
        from doc_selecter.windows import app
        with patch.object(app, 'webview_available', return_value=True), patch.object(app, 'notice') as notice:
            self.assertTrue(app.choose_webview())
            notice.assert_not_called()


class WindowsBuildPolicyTests(unittest.TestCase):
    def test_windows_source_whitelist_complete_and_no_state(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('windows_builder', REPO / 'scripts/build_windows_distributable.py')
        builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
        self.assertEqual(set(builder.WINDOWS), {p.stem for p in (REPO / 'doc_selecter/windows').glob('*.py')})
        self.assertEqual(set(builder.CORE), {p.stem for p in (REPO / 'doc_selecter').glob('*.py')})
        self.assertFalse(any('tests/' in p or 'config.json' in p or p.startswith('build/') for p in builder.SOURCES))

    def test_manual_workflow_has_no_publish_permission(self):
        workflow = (REPO / '.github/workflows/windows-preview.yml').read_text(encoding='utf-8')
        self.assertIn('workflow_dispatch:', workflow)
        self.assertIn('contents: read', workflow)
        self.assertNotIn('contents: write', workflow)
        self.assertNotIn('pull_request_target', workflow)
        self.assertNotIn('gh release', workflow)
        self.assertIn('scripts/test_windows_background.py', workflow)
        self.assertNotIn('**/*.log', workflow)
