"""Frozen packaging safety contracts; no visible windows or user paths."""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys
from unittest.mock import patch
from tests.harness import SandboxTestCase

REPO = Path(__file__).resolve().parents[1]
def module(name):
    spec=importlib.util.spec_from_file_location(name,REPO/'scripts'/(name+'.py'))
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
builder=module('build_distributable');auditor=module('audit_macos_bundle')

class DistributableTests(SandboxTestCase):
    def test_existing_output_and_app_are_never_overwritten(self):
        output=self.sandbox.root/'product';output.mkdir();marker=output/'retain';marker.write_bytes(b'ARTIFICIAL')
        with patch.object(builder,'validate_environment'):
            with self.assertRaises(FileExistsError):builder.build(output)
        self.assertEqual(marker.read_bytes(),b'ARTIFICIAL')
        self.assertEqual(list(output.iterdir()),[marker])

    def test_external_python_environment_is_rejected(self):
        with patch.object(builder.sys,'prefix','/artificial/base'),patch.object(builder.sys,'base_prefix','/artificial/base'),patch.object(builder.platform,'machine',return_value='arm64'),patch.object(builder.sys,'platform','darwin'):
            with self.assertRaisesRegex(RuntimeError,'隔离'):builder.validate_environment(json.loads(builder.LOCK_FILE.read_text()))

    def test_runtime_pin_has_https_digest_and_all_license_sources(self):
        lock=json.loads(builder.LOCK_FILE.read_text())
        for key in ('runtime','license_archive'):
            self.assertTrue(lock[key]['url'].startswith('https://github.com/astral-sh/python-build-standalone/releases/download/20261013/'))
            self.assertRegex(lock[key]['sha256'],r'^[a-f0-9]{64}$')
        self.assertEqual(lock['architecture'],'arm64')
        self.assertEqual(lock['python_version'],'3.12.15')
        self.assertIn('LICENSE.cpython.txt',lock['license_files']);self.assertIn('LICENSE.tcl.txt',lock['license_files'])
        for n in lock['license_files']:self.assertTrue((REPO/'packaging/licenses'/n).is_file())

    def test_system_library_requires_no_filesystem_probe(self):
        with patch.object(Path,'exists',side_effect=AssertionError('never probe user or system path')):
            self.assertEqual(auditor.resolve_dependency('/usr/lib/libSystem.B.dylib',self.sandbox.root/'binary',self.sandbox.root,[]),'system')

    def test_absolute_developer_dependency_is_rejected(self):
        with self.assertRaises(ValueError):auditor.resolve_dependency('/artificial/python/libpython.dylib',self.sandbox.root/'binary',self.sandbox.root,[])

    def test_missing_bundle_dependency_is_rejected(self):
        with self.assertRaises(ValueError):auditor.resolve_dependency('@rpath/absent.dylib',self.sandbox.root/'binary',self.sandbox.root,['@loader_path'])

    def test_internal_relative_library_resolves(self):
        app=self.sandbox.root/'App';(app/'Contents/MacOS').mkdir(parents=True);(app/'Contents/Frameworks').mkdir()
        lib=app/'Contents/Frameworks/libpython.dylib';lib.write_bytes(b'ARTIFICIAL')
        self.assertEqual(auditor.resolve_dependency('@loader_path/../Frameworks/libpython.dylib',app/'Contents/MacOS/run',app,[]),'Contents/Frameworks/libpython.dylib')

    def test_library_link_escape_is_rejected(self):
        app=self.sandbox.root/'App';app.mkdir();outside=self.sandbox.root/'outside';outside.write_bytes(b'ARTIFICIAL')
        (app/'linked.dylib').symlink_to(outside)
        with self.assertRaises(ValueError):auditor.resolve_dependency('@loader_path/linked.dylib',app/'binary',app,[])

    def test_frozen_early_diagnosis_guard_does_not_enter_real_mode(self):
        with patch.dict(os.environ,{},clear=True),patch.object(sys,'argv',['App','--diagnose']):
            with self.assertRaises(SystemExit) as caught:runpy.run_path(str(REPO/'packaging/frozen_runtime.py'))
        self.assertEqual(caught.exception.code,64)

    def test_damaged_tcl_resource_copy_has_clear_diagnosis_no_popup(self):
        from doc_selecter import app_startup
        with patch.dict(os.environ,{'DOCSELECTER_TEST_ROOT':str(self.sandbox.root)}),patch.object(sys,'argv',['App','--diagnose']),patch.object(sys,'_MEIPASS',str(self.sandbox.root),create=True),patch.object(app_startup,'_failure_notice',side_effect=AssertionError('no visible popup')):
            with self.assertRaises(SystemExit) as caught:runpy.run_path(str(REPO/'packaging/frozen_runtime.py'))
        self.assertEqual(caught.exception.code,1)
        text=(self.sandbox.state/'app-launch.log').read_text();self.assertIn('重新复制完整应用',text);self.assertIn('无需',text.replace('不需要','无需'))
        self.assertFalse((self.sandbox.state/'plans').exists())

    def test_valid_frozen_data_guard_does_not_generate_plan_or_log(self):
        for n in ('_tcl_data/init.tcl','_tk_data/tk.tcl'):
            p=self.sandbox.root/n;p.parent.mkdir();p.write_bytes(b'ARTIFICIAL')
        with patch.dict(os.environ,{'DOCSELECTER_TEST_ROOT':str(self.sandbox.root)}),patch.object(sys,'argv',['App','--diagnose']),patch.object(sys,'_MEIPASS',str(self.sandbox.root),create=True):runpy.run_path(str(REPO/'packaging/frozen_runtime.py'))
        self.assertFalse((self.sandbox.state/'app-launch.log').exists())
        self.assertFalse((self.sandbox.state/'plans').exists())

    def test_old_local_packaging_whitelist_stays_identical(self):
        baseline={p.name for p in (REPO/'doc_selecter').glob('*.py')}
        self.assertEqual(set(builder.MODULES),baseline)
        self.assertEqual(len(builder.MODULES),24)

    def test_runtime_archive_hash_mismatch_refuses_before_execution(self):
        prep=module('prepare_macos_build')
        p=self.sandbox.root/'runtime.tar.gz';p.write_bytes(b'ARTIFICIAL NOT A RUNTIME')
        with self.assertRaisesRegex(ValueError,'SHA256'):prep.verify_archive(p,'0'*64)
        self.assertEqual(p.read_bytes(),b'ARTIFICIAL NOT A RUNTIME')

    def test_preparation_preserves_existing_output(self):
        prep=module('prepare_macos_build')
        p=self.sandbox.root/'environment';p.mkdir();marker=p/'retain';marker.write_bytes(b'ARTIFICIAL')
        with patch.object(prep.urllib.request,'urlopen',side_effect=AssertionError('no network for collision')):
            with self.assertRaises(FileExistsError):prep.prepare(p)
        self.assertEqual(marker.read_bytes(),b'ARTIFICIAL')

    def test_external_executable_path_escape_is_rejected(self):
        app=self.sandbox.root/'App';(app/'Contents/MacOS').mkdir(parents=True)
        external=self.sandbox.root/'outside.dylib';external.write_bytes(b'ARTIFICIAL')
        with self.assertRaises(ValueError):auditor.resolve_dependency('@executable_path/../../../outside.dylib',app/'Contents/MacOS/run',app,[])

    def test_release_notes_remain_accurate_before_and_after_publication(self):
        text=builder.release_notes(json.loads(builder.LOCK_FILE.read_text()))
        self.assertIn('0.1.0',text);self.assertIn('公开预览版',text)
        for temporary in ('候选','尚未发布','尚未正式发布','待主控','待独立核验'):
            self.assertNotIn(temporary,text)
        for boundary in ('arm64','11.0','27.0','Developer ID','公证','Gatekeeper','干净Mac','原生','SHA256SUMS'):
            self.assertIn(boundary,text)
        self.assertNotIn('/releases/download/',text)

    def test_installation_guide_has_durable_status_and_safety_boundaries(self):
        text=builder.installation_guide(json.loads(builder.LOCK_FILE.read_text()))
        self.assertIn('0.1.0 公开预览版',text)
        for temporary in ('候选','尚未发布','尚未正式发布','待主控','待独立核验'):
            self.assertNotIn(temporary,text)
        for boundary in ('arm64','11.0','27.0','Developer ID','未公证','Gatekeeper','干净Mac','原生','SHA256SUMS','不要未经确认替换'):
            self.assertIn(boundary,text)
        self.assertNotIn('/releases/download/',text)
