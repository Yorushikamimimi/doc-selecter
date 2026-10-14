"""Local App packaging contracts; no real-user directory or visible-window tests."""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import plistlib
import subprocess
import unittest
from unittest.mock import patch

from tests.harness import SandboxTestCase
from doc_selecter import app_startup as startup
from doc_selecter.config import ConfigError, TEST_ROOT_ENV

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('docsel_build_app', ROOT/'scripts/build_macos_app.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class AppStartupTests(SandboxTestCase):
    def test_injected_root_takes_priority_and_does_not_read_config(self):
        with patch('builtins.open', side_effect=AssertionError('config must not be read')):
            settings = startup.load_app_settings({TEST_ROOT_ENV: str(self.sandbox.root)}, _home='/never/use/home')
        self.assertTrue(settings.sandboxed)
        self.assertEqual(settings.downloads_dir, str(self.sandbox.downloads))
        self.assertEqual(settings.state_dir, str(self.sandbox.state))

    def test_safe_defaults_use_artificial_home_placeholder_not_downloads(self):
        home = self.sandbox.root/'artificial-home'; home.mkdir()
        with patch('builtins.open', side_effect=AssertionError('config must not be read')):
            settings = startup.load_app_settings({}, _home=str(home))
        self.assertEqual((settings.downloads_dir, settings.state_dir), startup.application_paths(str(home)))
        self.assertEqual(Path(settings.downloads_dir).name, '未选择来源')
        self.assertFalse((home/'Downloads').exists())
        self.assertEqual(list(Path(settings.downloads_dir).iterdir()), [])
        self.assertFalse(settings.sandboxed)

    def test_safe_default_rejects_linked_support_parent(self):
        home = self.sandbox.root/'artificial-home'; home.mkdir()
        other = self.sandbox.root/'alternate'; other.mkdir()
        (home/'Library').symlink_to(other, target_is_directory=True)
        with self.assertRaises(OSError): startup.load_app_settings({}, _home=str(home))
        self.assertEqual(list(other.iterdir()), [])

    def test_injected_state_link_is_refused(self):
        root = self.sandbox.root/'case'; root.mkdir(); (root/'Downloads').mkdir()
        (root/'state').symlink_to(self.sandbox.state, target_is_directory=True)
        with self.assertRaises(OSError): startup.load_app_settings({TEST_ROOT_ENV:str(root)})

    def test_raw_parent_traversal_is_refused(self):
        with self.assertRaises(ConfigError): startup._directory(str(self.sandbox.root)+'/../elsewhere', create=True)

    def test_instance_lock_refuses_duplicate_without_using_core_lock(self):
        first = startup.AppInstance(str(self.sandbox.state))
        try:
            with self.assertRaises(BlockingIOError): startup.AppInstance(str(self.sandbox.state))
            self.assertFalse((self.sandbox.state/'lock').exists())
        finally: first.close()
        again = startup.AppInstance(str(self.sandbox.state)); again.close(); again.close()

    def test_instance_lock_never_follows_link(self):
        target = self.sandbox.state/'untouched'; target.write_bytes(b'ARTIFICIAL')
        (self.sandbox.state/'app-instance.lock').symlink_to(target)
        with self.assertRaises(OSError): startup.AppInstance(str(self.sandbox.state))
        self.assertEqual(target.read_bytes(), b'ARTIFICIAL')

    def test_diagnostic_log_never_follows_link(self):
        target = self.sandbox.state/'untouched'; target.write_bytes(b'ARTIFICIAL')
        (self.sandbox.state/'app-launch.log').symlink_to(target)
        self.assertIsNone(startup._write_diagnostic('failed', {TEST_ROOT_ENV:str(self.sandbox.root)}))
        self.assertEqual(target.read_bytes(), b'ARTIFICIAL')

    def test_diagnostic_mode_without_injection_refuses_before_any_path_access(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(startup, 'load_app_settings', side_effect=AssertionError), patch.object(startup, '_failure_notice', side_effect=AssertionError):
            self.assertEqual(startup.app_main(['--diagnose']), 64)

    def test_failure_notice_uses_literal_arguments(self):
        text = '人工 "中文" $() \\ 路径'
        with patch.object(startup.subprocess, 'run') as run:
            startup._failure_notice(text, '/artificial/path')
        args = run.call_args.args[0]
        self.assertEqual(args[0], '/usr/bin/osascript')
        self.assertNotIn(text, args[2])
        self.assertEqual(args[-1], text+'\n诊断记录：/artificial/path')

    def test_tk_failure_records_diagnosis_and_releases_instance_without_popup(self):
        with patch.dict(os.environ, {TEST_ROOT_ENV: str(self.sandbox.root)}), patch('doc_selecter.gui.build_app', side_effect=RuntimeError('ARTIFICIAL_TK_FAILURE')), patch.object(startup, '_failure_notice', side_effect=AssertionError('no test popup')):
            self.assertEqual(startup.app_main(['--diagnose']), 1)
        log = (self.sandbox.state/'app-launch.log').read_text()
        self.assertIn('ARTIFICIAL_TK_FAILURE', log)
        self.assertIn('Python／Tk', log)
        again = startup.AppInstance(str(self.sandbox.state)); again.close()
        self.assertFalse((self.sandbox.state/'plans').exists())

    def test_native_diagnosis_has_same_test_root_guard(self):
        source = (ROOT/'packaging/launcher.m').read_text()
        self.assertIn('诊断模式必须注入 DOCSELECTER_TEST_ROOT', source)
        self.assertLess(source.index('诊断模式必须注入'), source.index('NSBundle *bundle'))
        self.assertIn('execv(args[0], args)', source)
        self.assertNotIn('system(', source)


class BuildContractsTests(SandboxTestCase):
    def test_whitelist_includes_runtime_and_excludes_config_state_tests(self):
        actual = {p.name for p in (ROOT/'doc_selecter').glob('*.py')}
        self.assertEqual(set(builder.MODULES), actual)
        self.assertEqual(len(builder.MODULES), len(set(builder.MODULES)))
        self.assertNotIn('config.json', builder.MODULES)
        for name in builder.MODULES: self.assertTrue((ROOT/'doc_selecter'/name).is_file())

    def test_existing_output_is_never_overwritten(self):
        output = self.sandbox.root/'build'; output.mkdir()
        app = output/(builder.NAME+'.app'); app.mkdir()
        marker = app/'retain'; marker.write_bytes(b'ARTIFICIAL EXISTING APP')
        with self.assertRaises(FileExistsError): builder.build(output)
        self.assertEqual(marker.read_bytes(), b'ARTIFICIAL EXISTING APP')
        self.assertEqual(list(app.iterdir()), [marker])

    def test_missing_python_fails_without_creating_output(self):
        output = self.sandbox.root/'build'
        with self.assertRaises(ValueError): builder.build(output, str(self.sandbox.root/'absent-python'))
        self.assertFalse(output.exists())

    def test_master_is_real_square_png_and_icon_sizes_are_standard(self):
        self.assertEqual(builder.png_size(ROOT/'assets/icons/file-organizer-master.png'), (1254,1254))
        self.assertEqual(builder.ICON_SIZES, ((16,1),(16,2),(32,1),(32,2),(128,1),(128,2),(256,1),(256,2),(512,1),(512,2)))

    def test_bundle_entry_resolves_own_resources(self):
        entry = (ROOT/'packaging/bundle_entry.py').read_text()
        self.assertIn('Path(__file__).resolve().parent', entry)
        self.assertIn('sys.path.insert(0, str(RESOURCES))', entry)
        self.assertNotIn(str(ROOT), entry)
