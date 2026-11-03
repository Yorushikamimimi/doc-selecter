"""Regression coverage for recursive basis, whole-second ages and archive privacy."""
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from tests.harness import SandboxTestCase
from doc_selecter import remaining_archive as remaining
from doc_selecter import planner
from doc_selecter.directory_archive import validate_structure
from doc_selecter.executor import recover_execution
from doc_selecter.undo import undo_plan
from doc_selecter.plan_hash import compute_plan_hash, canonical_bytes
from doc_selecter.rules import normalize_extension, is_sensitive_relative_path
from doc_selecter.ui_controller import Controller

R1 = 'milestone6-finer-subfolders-r1'
OLD_REMAINING = 'milestone5-remaining-archive-r1'


class SmallFixTests(SandboxTestCase):
    def test_recursive_basis_only_category_top_level_keeps_subfolder(self):
        for name in ('IMG_1234.heic', '课程.pdf'):
            self.sandbox.add_file(name)
            self.sandbox.add_file('资料/' + name)
        plan = planner.build_plan(self.sandbox.settings, recursive=True, scope_dirs=['资料']).plan
        for op in plan['operations']:
            if '/' in op['rel_path']:
                self.assertEqual(op['classification_basis'],
                                 f"扩展名 {normalize_extension(op['rel_path'])} → {op['category']}")
            else:
                self.assertIn(op['subfolder'], op['classification_basis'])
        planner.assert_new_move_admissible(self.sandbox.settings, plan)
        validate_structure(self.sandbox.settings, plan)

    def test_r2_recursive_rehashed_basis_tamper_rejected(self):
        self.sandbox.add_file('资料/IMG_1234.heic')
        plan = planner.build_plan(self.sandbox.settings, recursive=True, scope_dirs=['资料']).plan
        self.assertEqual(plan['rules_version'], 'milestone6-finer-subfolders-r2')
        plan['operations'][0]['classification_basis'] = '名字是 IMG_ + 数字，格式 HEIC → 手机照片'
        plan['plan_hash'] = compute_plan_hash(plan)
        with self.assertRaises(planner.PlanError):
            planner.assert_new_move_admissible(self.sandbox.settings, plan)
        with self.assertRaises(planner.PlanError):
            validate_structure(self.sandbox.settings, plan)

    def historical_roundtrip(self, plan, *, interrupted):
        # Existing destination directories need no synthetic layout-create events.
        from doc_selecter.directory_archive import identity
        for op in plan['operations']:
            Path(op['dest']).parent.mkdir(parents=True, exist_ok=True)
        plan['dir_identities']['target_root'] = identity(plan['target_root'])
        for rel in plan['dir_identities']['dest_dirs']:
            plan['dir_identities']['dest_dirs'][rel] = identity(plan['target_root'] + '/' + rel)
        plan['plan_hash'] = compute_plan_hash(plan)
        planner.write_plan(self.sandbox.settings, plan)
        before = self.sandbox.plan_file(plan['plan_id']).read_bytes()
        # Recreate persisted pre-upgrade evidence, without authorizing new old-rule moves.
        from doc_selecter.journal import make_event
        for op in plan['operations']:
            journal = self.sandbox.journal(plan['plan_id'])
            journal.append(make_event('intent', op['op_id'], source=op['source'],
                dest=op['dest'], category=op['category'], identity=op['identity'],
                plan_hash=plan['plan_hash']))
            Path(op['dest']).parent.mkdir(parents=True, exist_ok=True)
            os.replace(op['source'], op['dest'])
        report = recover_execution(self.sandbox.settings, plan['plan_id'])
        self.assertEqual(report['unknown'], [])
        self.assertTrue(report['recovered'], report)
        if not interrupted:
            self.assertEqual(recover_execution(self.sandbox.settings, plan['plan_id'])['unknown'], [])
        self.assertEqual(planner.load_verified_plan(self.sandbox.settings,
            plan['plan_id'], display_only=True), plan)
        with self.assertRaises(planner.MoveAdmissionRefused):
            planner.load_verified_plan(self.sandbox.settings, plan['plan_id'], for_new_move=True)
        with self.assertRaises(planner.MoveAdmissionRefused):
            planner.write_confirmation(self.sandbox.settings, plan['plan_id'], plan['plan_hash'])
        self.assertEqual(undo_plan(self.sandbox.settings, plan['plan_id'], confirm=True).restored,
                         len(plan['operations']))
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(), before)
        for op in plan['operations']:
            self.assertIdentityMatches(op['source'], op['identity'])
            self.assertFalse(Path(op['dest']).exists())

    def test_r1_plan_view_recover_undo_but_no_new_move(self):
        self.sandbox.add_file('资料/IMG_1234.heic')
        self.sandbox.add_file('资料/课程.pdf')
        plan = planner.build_plan(self.sandbox.settings, recursive=True, scope_dirs=['资料']).plan
        plan['rules_version'] = R1
        old_basis = {'IMG_1234.heic': '名字是 IMG_ + 数字，格式 HEIC → 手机照片',
                     '课程.pdf': '名字里有「课程」→ 学校课程'}
        for op in plan['operations']:
            name = op['rel_path'].rsplit('/', 1)[-1]
            op['classification_basis'] = old_basis[name]
        plan['plan_hash'] = compute_plan_hash(plan)
        self.historical_roundtrip(plan, interrupted=True)

    def installer_rows(self, age_seconds):
        now_ns = 1_800_000_000_750_000_000
        path = self.sandbox.add_file('安装.dmg')
        os.utime(path, ns=(now_ns - age_seconds * 1_000_000_000,) * 2)
        controller = Controller(self.sandbox.settings)
        self.addCleanup(controller.close)
        plan = planner.build_plan(self.sandbox.settings, now=now_ns / 1_000_000_000).plan
        before = canonical_bytes(plan)
        rows = controller._rows(plan, controller.history)
        self.assertEqual(canonical_bytes(plan), before)
        return rows[0]['hints']

    def test_installer_exact_30_days_with_fractional_mtime(self):
        self.assertEqual(self.installer_rows(30 * 86400),
                         [{'kind': 'old_installer', 'text': '放了 30 天，可能已经装过'}])

    def test_installer_under_30_days_has_no_hint(self):
        self.assertEqual(self.installer_rows(30 * 86400 - 1), [])
        self.assertEqual(self.installer_rows(29 * 86400 + 23 * 3600), [])

    def test_installer_over_30_days_floors_correctly(self):
        self.assertEqual(self.installer_rows(42 * 86400),
                         [{'kind': 'old_installer', 'text': '放了 42 天，可能已经装过'}])

    def test_remaining_encoded_sensitive_excluded_before_stat_and_not_persisted(self):
        names = [quote('身份证') + '.pdf', quote('私密')]
        self.sandbox.add_file(names[0])
        self.sandbox.add_dir(names[1])
        self.sandbox.add_file('safe.pdf')
        original = os.stat
        def guarded(path, *args, **kwargs):
            self.assertFalse(any(name in str(path) for name in names), 'sensitive stat')
            return original(path, *args, **kwargs)
        with patch('os.stat', side_effect=guarded):
            discovery = remaining.discover_remaining(self.sandbox.settings)
            plan = remaining.build_remaining_plan(self.sandbox.settings, selection=['safe.pdf']).plan
        self.assertEqual(discovery.excluded_counts.get('疑似私人项目'), 2)
        self.assertEqual([c['rel_path'] for c in discovery.candidates], ['safe.pdf'])
        for name in names + ['身份证', '私密']:
            self.assertNotIn(name, json.dumps(plan, ensure_ascii=False))
        with self.assertRaises(planner.PlanError):
            remaining.relative(names[0])

    def test_old_remaining_encoded_plan_view_recover_undo_but_no_new_move(self):
        name = quote('身份证') + '.pdf'
        self.sandbox.add_file(name)
        # Generate the old schema4 contract with its original name-only screening.
        def legacy_screen(value, **kwargs):
            return is_sensitive_relative_path(value, legacy=True)
        with patch.object(remaining, 'RULES_VERSION', OLD_REMAINING), \
                patch.object(remaining, 'is_sensitive_relative_path', legacy_screen):
            plan = remaining.build_remaining_plan(self.sandbox.settings, selection=[name]).plan
        self.assertEqual(plan['rules_version'], OLD_REMAINING)
        self.historical_roundtrip(plan, interrupted=False)

    def test_current_remaining_rehashed_encoded_name_refused_before_metadata(self):
        name = quote('身份证') + '.pdf'
        self.sandbox.add_file(name)
        def legacy_screen(value, **kwargs):
            return is_sensitive_relative_path(value, legacy=True)
        with patch.object(remaining, 'RULES_VERSION', OLD_REMAINING), \
                patch.object(remaining, 'is_sensitive_relative_path', legacy_screen):
            plan = remaining.build_remaining_plan(self.sandbox.settings, selection=[name]).plan
        plan['rules_version'] = remaining.RULES_VERSION
        plan['plan_hash'] = compute_plan_hash(plan)
        planner.write_plan(self.sandbox.settings, plan)
        settings = self.sandbox.settings
        with patch('os.stat', side_effect=AssertionError('metadata forbidden')), \
                patch('os.lstat', side_effect=AssertionError('metadata forbidden')):
            with self.assertRaisesRegex(planner.PlanError, '不展示名称'):
                planner.load_verified_plan(settings, plan['plan_id'], for_new_move=True)
