"""Unicode 目录身份键：真实沙盒闭环、旧 v2 编码兼容与拒绝边界。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

from tests.harness import SandboxTestCase
from tests import test_gui as gui_tests
from doc_selecter.atomic_move import AtomicMover, dir_identity, lstat_identity
from doc_selecter.executor import execute_plan, recover_execution
from doc_selecter.journal import make_event
from doc_selecter.plan_hash import verify_plan_hash, compute_plan_hash
from doc_selecter.planner import (
    PlanError, build_plan, capture_dir_identities, confirmation_path,
    verify_plan_binding, write_confirmation, write_plan,
)
from doc_selecter.undo import UndoRefused, undo_plan


# 独立 runner 可保存实际身份/日志/回调证据；通过与否仍由下面的断言决定。
EVIDENCE = []


class UnicodeFixture:
    def seed(self, form="NFD", existing=False):
        self.name = unicodedata.normalize(form, "Cafe" + chr(0x0301))
        names = [self.name + "/deep/资料/说明.pdf", self.name + "/deep/资料/第二.pdf",
                 "中文目录/深层/说明.pdf", "ordinary.pdf", "report" + chr(92) + "2026.pdf"]
        self.originals = {}
        for i, name in enumerate(names):
            path = self.sandbox.add_file(name, ("PAYLOAD-" + str(i)).encode())
            self.originals[name] = dict(identity=lstat_identity(str(path)).as_dict(),
                                        content=path.read_bytes(), basename=path.name)
        if existing:
            for name in names:
                (self.sandbox.target_dir("文档资料") / Path(name).parent).mkdir(parents=True, exist_ok=True)
        self.source_dirs_before = {
            str(p.relative_to(self.sandbox.downloads)): dir_identity(str(p))
            for p in self.sandbox.downloads.rglob("*")
            if p.is_dir() and "下载分类" not in p.parts
        }
        return [self.name, "中文目录"]

    def save_plan(self, *, legacy=False):
        plan = build_plan(self.sandbox.settings, scope_dirs=[self.name, "中文目录"], recursive=True).plan
        if legacy:
            # 模拟旧 capture 的原始 NFD 键，仅转换键；保留原 dev/inode 和哈希。
            for field in ("source_dirs", "dest_dirs"):
                plan["dir_identities"][field] = {
                    unicodedata.normalize("NFD", key): value
                    for key, value in plan["dir_identities"][field].items()
                }
        self.assertTrue(verify_plan_hash(plan))
        write_plan(self.sandbox.settings, plan)
        write_confirmation(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        return plan

    def immutable_bytes(self, plan):
        paths = [self.sandbox.plan_file(plan["plan_id"]),
                 Path(confirmation_path(self.sandbox.settings, plan["plan_id"]))]
        return {str(path): path.read_bytes() for path in paths}

    def assert_immutable(self, before):
        for path, data in before.items():
            self.assertEqual(Path(path).read_bytes(), data)

    def assert_targets(self, plan):
        actual = {}
        for op in plan["operations"]:
            self.assertNotExists(op["source"])
            self.assertIdentityMatches(op["dest"], op["identity"])
            self.assertEqual(Path(op["dest"]).read_bytes(), self.originals[op["rel_path"]]["content"])
            actual[op["rel_path"]] = lstat_identity(op["dest"]).as_dict()
        return actual

    def assert_restored(self, plan, *, route, target_identities):
        actual = {}
        for rel, before in self.originals.items():
            path = self.sandbox.downloads / rel
            self.assertIdentityMatches(path, before["identity"])
            self.assertEqual(path.read_bytes(), before["content"])
            self.assertEqual(path.name, before["basename"])
            self.assertIn(before["basename"], os.listdir(path.parent))
            actual[rel] = lstat_identity(str(path)).as_dict()
        for op in plan["operations"]:
            self.assertNotExists(op["dest"])
        for rel, identity in self.source_dirs_before.items():
            self.assertEqual(dir_identity(str(self.sandbox.downloads / rel)), identity)
        logs = {kind: self.sandbox.journal(plan["plan_id"], kind).classify().classification
                for kind in ("execution", "undo")}
        self.assertEqual(logs, dict(execution="healthy", undo="healthy"))
        EVIDENCE.append(dict(route=route, sandbox=str(self.sandbox.root), plan_id=plan["plan_id"],
                             keys=plan["dir_identities"], target_identities=target_identities,
                             restored_identities=actual, logs=logs))

    def roundtrip(self, form, existing, *, legacy=False):
        self.seed(form, existing)
        plan = self.save_plan(legacy=legacy)
        immutable = self.immutable_bytes(plan)
        if not legacy:
            for field in ("source_dirs", "dest_dirs"):
                for key in plan["dir_identities"][field]:
                    self.assertEqual(key, unicodedata.normalize("NFC", key))
        verify_plan_binding(self.sandbox.settings, plan)
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual((result.moved, result.failed, result.stopped), (5, 0, False), result.outcomes)
        targets = self.assert_targets(plan)
        recovery = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(recovery["unknown"], [])
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual((undo.restored, undo.blocked, undo.unknown), (5, 0, 0), undo.entries)
        self.assert_immutable(immutable)
        self.assert_restored(plan, route=f"API/{form}/existing={existing}/legacy={legacy}",
                             target_identities=targets)


class UnicodeDirectoryTests(UnicodeFixture, SandboxTestCase):
    def test_nfd_missing_target_chain_roundtrip(self):
        self.roundtrip("NFD", False)

    def test_nfd_existing_target_chain_roundtrip(self):
        self.roundtrip("NFD", True)

    def test_nfc_missing_target_chain_roundtrip(self):
        self.roundtrip("NFC", False)

    def test_nfc_existing_target_chain_roundtrip(self):
        self.roundtrip("NFC", True)

    def test_legacy_nfd_keys_missing_target_chain_roundtrip(self):
        self.roundtrip("NFD", False, legacy=True)

    def test_legacy_nfd_keys_existing_target_chain_roundtrip(self):
        self.roundtrip("NFD", True, legacy=True)

    def test_replaced_source_and_existing_destination_each_level_refused(self):
        self.seed(existing=True)
        plan = self.save_plan(legacy=True)
        immutable = self.immutable_bytes(plan)
        for field, root in (("source_dirs", self.sandbox.downloads),
                            ("dest_dirs", self.sandbox.target_dir("文档资料"))):
            for rel in (self.name, self.name + "/deep", self.name + "/deep/资料"):
                with self.subTest(field=field, level=rel):
                    path, saved = root / rel, self.sandbox.root / "saved-parent"
                    path.rename(saved)
                    path.mkdir()
                    try:
                        with self.assertRaisesRegex(PlanError, "身份与计划不一致"):
                            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
                        self.assertEqual(self.sandbox.journal(plan["plan_id"]).read_events()[0], [])
                    finally:
                        path.rmdir()
                        saved.rename(path)
        self.assert_immutable(immutable)
        for op in plan["operations"]:
            self.assertIdentityMatches(op["source"], op["identity"])

    def test_symlink_source_and_destination_parent_chains_refused(self):
        self.seed(existing=True)
        plan = self.save_plan(legacy=True)
        for root in (self.sandbox.downloads, self.sandbox.target_dir("文档资料")):
            for rel in (self.name, self.name + "/deep", self.name + "/deep/资料"):
                with self.subTest(root=str(root), level=rel):
                    path, saved = root / rel, self.sandbox.root / "saved-parent"
                    path.rename(saved)
                    path.symlink_to(saved, target_is_directory=True)
                    try:
                        with self.assertRaises(PlanError):
                            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
                        self.assertEqual(self.sandbox.journal(plan["plan_id"]).read_events()[0], [])
                    finally:
                        path.unlink()
                        saved.rename(path)
        for op in plan["operations"]:
            self.assertIdentityMatches(op["source"], op["identity"])

    def conflict(self, *, same_identity):
        self.seed(existing=True)
        for field in ("source_dirs", "dest_dirs"):
            with self.subTest(field=field, same_identity=same_identity):
                plan = build_plan(self.sandbox.settings, scope_dirs=[self.name], recursive=True).plan
                key = next(k for k in plan["dir_identities"][field] if "Café" in k)
                identities = plan["dir_identities"][field]
                identity = identities[key]
                identities[unicodedata.normalize("NFD", key)] = identity if same_identity else [identity[0], identity[1] + 1]
                plan["plan_hash"] = compute_plan_hash(plan)
                write_plan(self.sandbox.settings, plan)
                with self.assertRaisesRegex(PlanError,"NFC 冲突或歧义"):
                    write_confirmation(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
                immutable = {str(self.sandbox.plan_file(plan["plan_id"])):self.sandbox.plan_file(plan["plan_id"]).read_bytes()}
                with self.assertRaisesRegex(PlanError, "NFC 冲突或歧义"):
                    execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
                with self.assertRaisesRegex(PlanError, "NFC 冲突或歧义"):
                    recover_execution(self.sandbox.settings, plan["plan_id"])
                with self.assertRaisesRegex(UndoRefused, "NFC 冲突或歧义"):
                    undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
                self.assert_immutable(immutable)
                self.assertEqual(self.sandbox.journal(plan["plan_id"]).read_events()[0], [])
                EVIDENCE.append(dict(route="collision-refusal", field=field, same_identity=same_identity,
                                     sandbox=str(self.sandbox.root), plan_id=plan["plan_id"]))
        for rel, before in self.originals.items():
            self.assertIdentityMatches(self.sandbox.downloads / rel, before["identity"])

    def test_conflicting_normalized_identity_keys_refused(self):
        self.conflict(same_identity=False)

    def test_duplicate_normalized_aliases_refused_even_with_same_identity(self):
        self.conflict(same_identity=True)

    def test_capture_refuses_ambiguous_raw_unicode_directory_keys(self):
        self.seed(existing=True)
        operations = [{"rel_path": form + "/deep/资料/说明.pdf", "category": "文档资料"}
                      for form in (self.name, unicodedata.normalize("NFC", self.name))]
        with self.assertRaisesRegex(PlanError, "NFC 冲突或歧义"):
            capture_dir_identities(self.sandbox.settings, operations)

    def test_absent_identity_key_still_stops_before_movement(self):
        self.seed()
        plan = build_plan(self.sandbox.settings, scope_dirs=[self.name], recursive=True).plan
        keys = plan["dir_identities"]["dest_dirs"]
        for key in list(keys):
            if "Café" in key:
                del keys[key]
        plan['plan_hash']=compute_plan_hash(plan)
        write_plan(self.sandbox.settings, plan)
        with self.assertRaisesRegex(PlanError,'父链身份'):
            write_confirmation(self.sandbox.settings, plan['plan_id'],plan['plan_hash'])
        with self.assertRaisesRegex(PlanError,'父链身份'):
            execute_plan(self.sandbox.settings,plan['plan_id'],plan['plan_hash'])
        self.assertEqual(self.sandbox.journal(plan['plan_id']).read_events()[0],[])
        for op in plan["operations"]:
            self.assertIdentityMatches(op["source"], op["identity"])

    def test_newly_created_parent_identity_is_reused_and_replacement_refused(self):
        self.seed()
        plan = self.save_plan()
        changed, saved = [], self.sandbox.root / "saved-runtime-parent"
        from doc_selecter import executor
        real_precheck = executor._precheck_source

        def precheck(operation, downloads_dir):
            if (unicodedata.normalize("NFC", operation["rel_path"]).startswith("Café/")
                    and operation["rel_path"].endswith("说明.pdf")):
                path = Path(operation["dest"]).parent
                path.rename(saved)
                path.mkdir()
                changed.append(path)
            return real_precheck(operation, downloads_dir)

        from unittest.mock import patch
        with patch.object(executor, "_precheck_source", precheck):
            result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual((result.moved, result.failed, result.stopped), (1, 1, True), result.outcomes)
        self.assertIn("目标父目录身份发生变化", result.stop_reason)
        self.assertEqual(len(changed), 1)
        changed[0].rmdir()
        saved.rename(changed[0])
        targets = {op["rel_path"]: lstat_identity(op["dest"]).as_dict()
                   for op in plan["operations"] if os.path.isfile(op["dest"])}
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 1)
        self.assert_restored(plan, route="runtime-replacement-refusal-and-undo", target_identities=targets)

    def old_history(self, *, completed):
        self.seed()
        from tests.legacy_material import r11_planner
        old=r11_planner()
        plan=old.build_plan(self.sandbox.settings,scope_dirs=[self.name,'中文目录'],recursive=True).plan
        for field in ('source_dirs','dest_dirs'):
            plan['dir_identities'][field]={unicodedata.normalize('NFD',k):v for k,v in plan['dir_identities'][field].items()}
        old.write_plan(self.sandbox.settings,plan)
        old.write_confirmation(self.sandbox.settings,plan['plan_id'],plan['plan_hash'])
        immutable = self.immutable_bytes(plan)
        journal = self.sandbox.journal(plan["plan_id"])
        for op in plan["operations"]:
            journal.append(make_event("intent", op["op_id"], source=op["source"], dest=op["dest"],
                                      category=op["category"], identity=op["identity"]))
            Path(op["dest"]).parent.mkdir(parents=True, exist_ok=True)
            AtomicMover().rename_excl(op["source"], op["dest"])
            if completed:
                journal.append(make_event("result", op["op_id"], outcome="moved", reason_code="OK",
                                          dest_identity=lstat_identity(op["dest"]).as_dict()))
            else:
                break  # 真实移动后 result 尚未落盘。
        targets = {op["rel_path"]: lstat_identity(op["dest"]).as_dict()
                   for op in plan["operations"] if os.path.isfile(op["dest"])}
        generation = Path(journal.generation_file(1))
        prefix = generation.read_bytes()
        recovery = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(recovery["unknown"], [])
        self.assertEqual(len(recovery["recovered"]), 0 if completed else 1)
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 5 if completed else 1)
        self.assertTrue(generation.read_bytes().startswith(prefix), "原日志字节只能按协议追加")
        self.assert_immutable(immutable)
        self.assert_restored(plan, route=f"legacy-history/completed={completed}", target_identities=targets)

    def test_legacy_completed_history_recovery_and_undo_preserve_evidence(self):
        self.old_history(completed=True)

    def test_legacy_interrupted_history_recovery_and_undo_preserve_evidence(self):
        self.old_history(completed=False)

    def test_public_cli_deep_unicode_batch_roundtrip(self):
        self.seed()
        calls = []

        def run(*args):
            proc = subprocess.run([sys.executable, "-m", "doc_selecter", *args],
                                  env=dict(os.environ, DOCSELECTER_TEST_ROOT=str(self.sandbox.root)),
                                  capture_output=True, text=True, timeout=30)
            calls.append(dict(args=args, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr))
            self.assertEqual(proc.returncode, 0, calls[-1])
            return proc

        run("plan", "--include-subfolder", self.name + "/deep", "--include-subfolder", "中文目录", "--recursive")
        plans = list((self.sandbox.state / "plans").glob("*.json"))
        self.assertEqual(len(plans), 1)
        plan = json.loads(plans[0].read_text())
        run("confirm", "--plan", plan["plan_id"])
        immutable = self.immutable_bytes(plan)
        run("execute", "--plan", plan["plan_id"], "--confirm", plan["plan_hash"])
        targets = self.assert_targets(plan)
        run("recover", "--plan", plan["plan_id"])
        run("undo", "--plan", plan["plan_id"], "--confirm")
        self.assert_immutable(immutable)
        self.assert_restored(plan, route="public-CLI", target_identities=targets)
        EVIDENCE[-1]["calls"] = calls


class UnicodeGuiTests(UnicodeFixture, SandboxTestCase):
    _stub_dialogs = gui_tests.GuiTests._stub_dialogs
    _restore_dialogs = gui_tests.GuiTests._restore_dialogs
    _build = gui_tests.GuiTests._build

    def setUp(self):
        super().setUp()
        self.assertTrue(gui_tests.DISPLAY_AVAILABLE, gui_tests._DISPLAY_ERROR)
        self.root = self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.on_close()
        elif self.root is not None:
            self.root.destroy()
        super().tearDown()

    def test_actual_gui_callbacks_unicode_preview_confirm_execute_recover_undo(self):
        self.seed()
        self.app = app = self._build()
        app.subfolder_var.set(True)
        app.on_toggle_subfolders()
        app.selected_scope = [self.name + "/deep", "中文目录"]
        app._on_scope_changed()
        app.on_preview()
        self.assertIsNotNone(app.plan)
        self.assertEqual(len(app.plan["operations"]), 5)
        self.assertEqual(str(app.confirm_button["state"]), "normal")
        app.on_confirm()
        self.assertEqual(app.confirmed_hash, app.plan["plan_hash"])
        self.assertEqual(str(app.execute_button["state"]), "normal")
        immutable = self.immutable_bytes(app.plan)
        app.on_execute()
        targets = self.assert_targets(app.plan)
        app.on_recover()
        app.on_undo()
        self.assertFalse([c for c in self.dialog_calls if c[0] == "error"], self.dialog_calls)
        self.assert_immutable(immutable)
        self.assert_restored(app.plan, route="GUI/real-Tk-callbacks/dialog-stubs", target_identities=targets)
        EVIDENCE[-1]["dialogs"] = self.dialog_calls

    def test_actual_gui_legacy_completed_history_undo(self):
        self.seed(existing=True)
        plan = self.save_plan(legacy=True)
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 5, result.outcomes)
        targets = self.assert_targets(plan)
        immutable = self.immutable_bytes(plan)
        journal = self.sandbox.journal(plan["plan_id"])
        generation = Path(journal.generation_file(1))
        log_bytes = generation.read_bytes()
        self.app = app = self._build()
        self.assertIsNone(app.confirmed_hash, "历史载入不得自动确认")
        self.assertEqual(str(app.undo_button["state"]), "normal")
        app.on_recover()
        app.on_undo()
        self.assertEqual(generation.read_bytes(), log_bytes)
        self.assert_immutable(immutable)
        self.assert_restored(plan, route="GUI/legacy-completed-history", target_identities=targets)
