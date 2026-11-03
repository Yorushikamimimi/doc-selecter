"""Explicit Windows-only checks, not discovered by the macOS background suite.

All material is created below the injected artificial root. No visible GUI, no
real Downloads, no system settings or user applications are accessed.
"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def artificial_junction(path, target):
    """Create a mount-point reparse fixture without a shell or elevation."""
    from doc_selecter.windows.native import api, close, error
    path.mkdir()
    name = str(target).replace('/', '\\')
    substitute = ('\\??\\' + name).encode('utf-16-le')
    printable = name.encode('utf-16-le')
    strings = substitute + b'\0\0' + printable + b'\0\0'
    data = struct.pack('<IHHHHHH', 0xA0000003, 8 + len(strings), 0,
                       0, len(substitute), len(substitute) + 2, len(printable)) + strings
    k, _ = api()
    handle = k.CreateFileW(str(path), 0x40000000, 0, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise error()
    try:
        ioctl = k.DeviceIoControl
        ioctl.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                          ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        ioctl.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(data); returned = ctypes.c_uint32()
        if not ioctl(handle, 0x900A4, buffer, len(data), None, 0, ctypes.byref(returned), None):
            raise error()
    finally:
        close(handle)


def interrupted(mode, plan_id, plan_hash):
    from doc_selecter.config import load_settings
    from doc_selecter.executor import execute_plan
    from doc_selecter.atomic_move import AtomicMover
    if mode == 'before':
        execute_plan(load_settings(), plan_id, plan_hash,
                     hook=lambda event, payload: os._exit(23) if event == 'before_move' else None)
    else:
        original = AtomicMover.rename_excl_at
        def crash(self, *args, **kwargs):
            original(self, *args, **kwargs)
            # Probe runs also call rename; only terminate on the fake test item.
            if args[1] == 'sample.pdf':
                os._exit(23)
        with patch.object(AtomicMover, 'rename_excl_at', crash):
            execute_plan(load_settings(), plan_id, plan_hash)


class WindowsNativeChecks(unittest.TestCase):
    def setUp(self):
        if os.name != 'nt' or not os.environ.get('DOCSELECTER_TEST_ROOT'):
            raise RuntimeError('Windows 原生检查必须在显式人工沙盒中运行。')
        self.root = Path(tempfile.mkdtemp(prefix='case-', dir=os.environ['DOCSELECTER_TEST_ROOT']))
        self.source, self.state = self.root / 'Downloads', self.root / 'state'
        self.source.mkdir(); self.state.mkdir()
        from doc_selecter.config import load_settings, ensure_state_dirs
        self.settings = load_settings(env={'DOCSELECTER_TEST_ROOT': str(self.root)})
        ensure_state_dirs(self.settings)

    def plan(self, *, confirm=True):
        from doc_selecter.planner import build_plan, write_plan, write_confirmation
        plan = build_plan(self.settings).plan
        write_plan(self.settings, plan)
        if confirm:
            write_confirmation(self.settings, plan['plan_id'], plan['plan_hash'])
        return plan

    def execute(self, plan, **kwargs):
        from doc_selecter.executor import execute_plan
        return execute_plan(self.settings, plan['plan_id'], plan['plan_hash'], **kwargs)

    def test_100_file_preview_move_undo(self):
        from doc_selecter.windows.smoke import run
        result = run()
        self.assertEqual(result['moved'], 100)
        self.assertEqual(result['restored'], 100)
        self.assertTrue(result['journals_healthy'])

    def test_cancelled_confirmation_cannot_move(self):
        from doc_selecter.executor import ExecutionRefused
        (self.source / 'sample.pdf').write_bytes(b'ARTIFICIAL')
        plan = self.plan(confirm=False)
        with self.assertRaises(ExecutionRefused):
            self.execute(plan)
        self.assertTrue((self.source / 'sample.pdf').is_file())
        self.assertFalse(Path(self.settings.target_root).exists())

    def test_destination_created_at_last_moment_is_not_overwritten(self):
        (self.source / 'sample.pdf').write_bytes(b'SOURCE')
        plan = self.plan()
        def hook(event, payload):
            if event == 'before_move':
                Path(payload['dest']).write_bytes(b'OTHER')
        result = self.execute(plan, hook=hook)
        self.assertEqual(result.moved, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(Path(plan['operations'][0]['dest']).read_bytes(), b'OTHER')
        self.assertEqual((self.source / 'sample.pdf').read_bytes(), b'SOURCE')

    def test_case_collision_is_not_overwritten(self):
        from doc_selecter.atomic_move import AtomicMover, DestinationExists, lstat_identity
        from doc_selecter.windows.native import open_directory
        (self.source / 'sample.pdf').write_bytes(b'SOURCE')
        (self.source / 'other.pdf').write_bytes(b'OTHER')
        expected = lstat_identity(str(self.source / 'sample.pdf')).as_dict()
        with open_directory(str(self.source)) as directory:
            with self.assertRaises(DestinationExists):
                AtomicMover().rename_excl_at(directory, 'sample.pdf', directory, 'OTHER.PDF', expected=expected)
        self.assertEqual((self.source / 'other.pdf').read_bytes(), b'OTHER')

    def test_changed_source_identity_is_rejected(self):
        (self.source / 'sample.pdf').write_bytes(b'SOURCE')
        plan = self.plan()
        def hook(event, payload):
            if event == 'before_move':
                Path(payload['source']).rename(self.source / 'old.pdf')
                Path(payload['source']).write_bytes(b'REPLACEMENT')
        result = self.execute(plan, hook=hook)
        self.assertEqual(result.moved, 0)
        self.assertEqual((self.source / 'sample.pdf').read_bytes(), b'REPLACEMENT')
        self.assertFalse(Path(plan['operations'][0]['dest']).exists())

    def test_open_file_is_skipped_without_closing_the_owner(self):
        from doc_selecter.windows.native import api, close, READ_ATTR
        (self.source / 'sample.pdf').write_bytes(b'SOURCE')
        plan = self.plan()
        k, _ = api()
        handle = k.CreateFileW(str(self.source / 'sample.pdf'), READ_ATTR, 1, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            result = self.execute(plan)
            self.assertEqual(result.moved, 0)
            self.assertEqual(result.skipped, 1)
        finally:
            close(handle)
        self.assertEqual((self.source / 'sample.pdf').read_bytes(), b'SOURCE')

    def test_ancestor_handle_blocks_rename(self):
        from doc_selecter.windows.native import open_directory
        nested = self.source / 'nested'; nested.mkdir()
        with open_directory(str(nested)):
            with self.assertRaises(OSError):
                self.source.rename(self.root / 'replaced')
        self.assertTrue(self.source.is_dir())

    def test_junction_never_followed(self):
        from doc_selecter.windows.native import open_directory
        outside = self.root / 'outside'; outside.mkdir()
        (outside / 'untouched.pdf').write_bytes(b'ARTIFICIAL')
        junction = self.source / 'junction'
        artificial_junction(junction, outside)
        with self.assertRaises(OSError):
            with open_directory(str(junction)):
                pass
        from doc_selecter.planner import build_plan
        plan = build_plan(self.settings, recursive=True, scope_dirs=['junction']).plan
        self.assertEqual(plan['operations'], [])
        self.assertEqual((outside / 'untouched.pdf').read_bytes(), b'ARTIFICIAL')

    def test_global_lock_is_exclusive_and_released(self):
        from doc_selecter.locking import GlobalLock, LockBusy
        first, second = GlobalLock(self.settings), GlobalLock(self.settings)
        handle = first.acquire(kind='test', action='test')
        try:
            with self.assertRaises(LockBusy):
                second.acquire(kind='test', action='test')
        finally:
            first.release(handle)
        handle = second.acquire(kind='test', action='test'); second.release(handle)

    def test_application_instance_lock_is_separate(self):
        from doc_selecter.app_startup import AppInstance
        from doc_selecter.locking import GlobalLock
        instance = AppInstance(str(self.state))
        try:
            with self.assertRaises(BlockingIOError):
                AppInstance(str(self.state))
            operation = GlobalLock(self.settings)
            handle = operation.acquire(kind='test', action='test'); operation.release(handle)
        finally:
            instance.close()

    def test_undo_original_position_occupied_preserves_both(self):
        from doc_selecter.undo import undo_plan
        (self.source / 'sample.pdf').write_bytes(b'SOURCE')
        plan = self.plan(); self.assertEqual(self.execute(plan).moved, 1)
        (self.source / 'sample.pdf').write_bytes(b'OTHER')
        result = undo_plan(self.settings, plan['plan_id'], confirm=True)
        self.assertEqual(result.restored, 0)
        self.assertEqual((self.source / 'sample.pdf').read_bytes(), b'OTHER')
        self.assertEqual(Path(plan['operations'][0]['dest']).read_bytes(), b'SOURCE')

    def test_process_exit_before_and_after_move_can_recover(self):
        from doc_selecter.executor import recover_execution, classify_journals
        from doc_selecter.undo import undo_plan
        for mode in ('before', 'after'):
            if mode == 'after':
                self.setUp()  # Independent artificial batch, old evidence retained.
            (self.source / 'sample.pdf').write_bytes(b'SOURCE')
            plan = self.plan()
            env = dict(os.environ, DOCSELECTER_TEST_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE='1')
            child = subprocess.run([sys.executable, '-B', '-c',
                'import sys; from tests.windows_native_checks import interrupted; interrupted(*sys.argv[1:])',
                mode, plan['plan_id'], plan['plan_hash']], cwd=REPO, env=env, capture_output=True)
            self.assertEqual(child.returncode, 23, child.stderr.decode(errors='replace'))
            recovered = recover_execution(self.settings, plan['plan_id'])
            self.assertEqual(recovered['unknown'], [])
            if mode == 'after':
                self.assertEqual(undo_plan(self.settings, plan['plan_id'], confirm=True).restored, 1)
            self.assertEqual((self.source / 'sample.pdf').read_bytes(), b'SOURCE')
            self.assertTrue(all(x['state'].healthy for x in classify_journals(self.settings, plan['plan_id']).values()))

    def test_log_failure_stops_before_user_move(self):
        from doc_selecter.journal import Journal, JournalWriteFailed
        (self.source / 'sample.pdf').write_bytes(b'SOURCE')
        plan = self.plan()
        original = Journal.append
        def fail(journal, event, *args, **kwargs):
            if event.get('event') == 'intent' and event.get('op_id', '').startswith('op-'):
                raise JournalWriteFailed('ARTIFICIAL FLUSH FAILURE')
            return original(journal, event, *args, **kwargs)
        with patch.object(Journal, 'append', fail):
            result = self.execute(plan)
        self.assertEqual(result.moved, 0)
        self.assertTrue(result.stopped)
        self.assertEqual((self.source / 'sample.pdf').read_bytes(), b'SOURCE')

    def test_archive_refused_in_backend(self):
        from doc_selecter.planner import build_plan, PlanError
        from doc_selecter.remaining_archive import discover_remaining
        with self.assertRaises(PlanError):
            build_plan(self.settings, archive_emptied_dirs=True)
        with self.assertRaises(PlanError):
            discover_remaining(self.settings)
