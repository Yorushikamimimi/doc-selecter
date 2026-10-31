"""步骤1：顶层文档资料按格式细分，递归文档保持原相对层级。"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import types
from pathlib import Path
import unittest

from tests.harness import Sandbox, SandboxTestCase
from tests.legacy_material import fixture_bytes
from doc_selecter.atomic_move import AtomicMover, lstat_identity
from doc_selecter.executor import ExecutionRefused, execute_plan, recover_execution
from doc_selecter.journal import make_event
from doc_selecter.plan_hash import compute_plan_hash
from doc_selecter.planner import (MoveAdmissionRefused, PlanError, build_plan,
                                  write_confirmation, write_plan)
from doc_selecter.snapshot import read_snapshot, snapshot_path
from doc_selecter.undo import UndoRefused, undo_plan


class DocumentSubcategoryTests(SandboxTestCase):
    LEGACY_FIXTURE_COMMIT = "2df1a18927a6653a886e146e54b7984ddc4912d1"

    def test_top_level_subcategories_and_recursive_legacy_layout(self) -> None:
        names = ("a.xls", "b.XLSX", "c.xlsm", "d.pdf", "e.doc", "f.DOCX",
                 "g.docm", "h.ppt", "i.PPTX", "j.pptm", "k.md", "l.MARKDOWN", "m.txt")
        for name in names:
            self.sandbox.add_file(name, name.encode())
        self.sandbox.add_file("deep/inside.pdf", b"deep")
        self.sandbox.add_file("csv.csv", b"csv")
        self.sandbox.add_file("unknown.xyz", b"unknown")
        plan = build_plan(self.sandbox.settings, recursive=True, scope_dirs=["deep"]).plan
        by_name = {op["rel_path"]: op for op in plan["operations"]}
        expected = {
            "a.xls": "Excel", "b.XLSX": "Excel", "c.xlsm": "Excel",
            "d.pdf": "PDF", "e.doc": "Word", "f.DOCX": "Word", "g.docm": "Word",
            "h.ppt": "PPT", "i.PPTX": "PPT", "j.pptm": "PPT",
            "k.md": "Markdown", "l.MARKDOWN": "Markdown", "m.txt": "其他文档",
        }
        for name, subcategory in expected.items():
            self.assertEqual(by_name[name]["subfolder"], subcategory)
            self.assertIn(f"文档资料/{subcategory}/{name}", by_name[name]["dest"])
        self.assertNotIn("subfolder", by_name["deep/inside.pdf"])
        self.assertTrue(by_name["deep/inside.pdf"]["dest"].endswith("文档资料/deep/inside.pdf"))
        self.assertEqual(by_name["csv.csv"]["category"], "代码与数据")
        self.assertEqual(by_name["unknown.xyz"]["category"], "待确认")
        totals = plan["category_totals"]["counts"]
        self.assertEqual(totals["文档资料"], len(expected) + 1)
        self.assertEqual(sum(totals.values()), plan["counts"]["included"])

    def test_new_layout_execute_undo_roundtrip(self) -> None:
        original = self.sandbox.add_file("roundtrip-report.pdf", b"artificial document")
        plan = build_plan(self.sandbox.settings).plan
        write_plan(self.sandbox.settings, plan)
        write_confirmation(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 1)
        op = plan["operations"][0]
        self.assertIdentityMatches(op["dest"], op["identity"])
        self.assertNotExists(original)
        undone = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undone.restored, 1)
        self.assertIdentityMatches(original, op["identity"])
        self.assertNotExists(op["dest"])

    def test_subcategory_target_tamper_refused_after_rehash(self) -> None:
        self.sandbox.add_file("report.pdf", b"artificial document")
        plan = build_plan(self.sandbox.settings).plan
        tampered = copy.deepcopy(plan)
        op = next(item for item in tampered["operations"] if item["rel_path"] == "report.pdf")
        op["subfolder"] = "Word"
        op["dest"] = op["dest"].replace("/PDF/", "/Word/")
        tampered["plan_hash"] = compute_plan_hash(tampered)
        write_plan(self.sandbox.settings, tampered)
        with self.assertRaises(PlanError):
            write_confirmation(self.sandbox.settings, tampered["plan_id"], tampered["plan_hash"])

    def test_current_plan_history_rejects_missing_or_null_subcategory_before_mutation(self) -> None:
        """当前规则的历史 recover/undo 也必须绑定顶层子类。"""
        self.sandbox.add_file("history-report.pdf", b"history")
        settings = self.sandbox.settings
        plan = build_plan(settings).plan
        write_plan(settings, plan)
        write_confirmation(settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(execute_plan(settings, plan["plan_id"], plan["plan_hash"]).moved, 1)
        plan_path = Path(settings.plans_dir) / f"{plan['plan_id']}.json"
        plan_bytes = plan_path.read_bytes()
        journal_events = {
            kind: self.sandbox.journal(plan["plan_id"], kind).read_events()[0]
            for kind in ("execution", "undo")
        }
        snapshot_bytes = Path(snapshot_path(settings, plan["plan_id"])).read_bytes()
        snapshot_before = read_snapshot(settings, plan["plan_id"])
        original = copy.deepcopy(plan)
        for value in ("missing", None):
            tampered = copy.deepcopy(original)
            op = tampered["operations"][0]
            if value == "missing":
                op.pop("subfolder", None)
            else:
                op["subfolder"] = None
            op["dest"] = op["dest"].replace("/PDF/", "/")
            tampered["plan_hash"] = compute_plan_hash(tampered)
            tampered_bytes = (json.dumps(tampered, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            plan_path.write_bytes(tampered_bytes)
            with self.assertRaises(PlanError):
                recover_execution(settings, plan["plan_id"])
            with self.assertRaises(UndoRefused):
                undo_plan(settings, plan["plan_id"], confirm=True)
            self.assertEqual(plan_path.read_bytes(), tampered_bytes)
            self.assertEqual(read_snapshot(settings, plan["plan_id"]), snapshot_before)
            self.assertEqual(Path(snapshot_path(settings, plan["plan_id"])).read_bytes(), snapshot_bytes)
            for kind, data in journal_events.items():
                self.assertEqual(self.sandbox.journal(plan["plan_id"], kind).read_events()[0], data)
            plan_path.write_bytes(plan_bytes)
        self.assertIdentityMatches(original["operations"][0]["dest"], original["operations"][0]["identity"])

    def test_current_plan_history_rejects_rehashed_alternate_subcategory_link(self) -> None:
        """重算指纹也不能把实际目标替换为链接目录。"""
        self.sandbox.add_file("linked-history.pdf", b"linked")
        settings = self.sandbox.settings
        plan = build_plan(settings).plan
        write_plan(settings, plan)
        write_confirmation(settings, plan["plan_id"], plan["plan_hash"])
        execute_plan(settings, plan["plan_id"], plan["plan_hash"])
        op = plan["operations"][0]
        pdf_dir = Path(op["dest"]).parent
        alternate = pdf_dir.parent / "ActualPDF"
        pdf_dir.rename(alternate)
        # 保留已执行的普通文件与五项身份；把原 PDF 目录名替换为指向
        # ActualPDF 的目录链接，专门覆盖 raw_dest 子类层漏检漏洞。
        pdf_dir.symlink_to(alternate, target_is_directory=True)
        tampered = copy.deepcopy(plan)
        tampered["operations"][0]["dest"] = str(alternate / Path(op["dest"]).name)
        tampered["plan_hash"] = compute_plan_hash(tampered)
        plan_path = Path(settings.plans_dir) / f"{plan['plan_id']}.json"
        before_snapshot = Path(snapshot_path(settings, plan["plan_id"])).read_bytes()
        before_journal = self.sandbox.journal(plan["plan_id"], "undo").read_events()[0]
        plan_path.write_text(json.dumps(tampered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises((PlanError, UndoRefused)):
            recover_execution(settings, plan["plan_id"])
        with self.assertRaises(UndoRefused):
            undo_plan(settings, plan["plan_id"], confirm=True)
        self.assertEqual(Path(snapshot_path(settings, plan["plan_id"])).read_bytes(), before_snapshot)
        self.assertEqual(self.sandbox.journal(plan["plan_id"], "undo").read_events()[0], before_journal)

    def test_current_rule_missing_or_null_subcategory_rejected_by_confirm_and_execute(self) -> None:
        """有效重算指纹下，确认和 execute 都因子类准入拒绝。"""
        self.sandbox.add_file("entry-report.pdf", b"entry")
        settings = self.sandbox.settings
        original = build_plan(settings).plan
        for value in ("missing", None):
            plan = copy.deepcopy(original)
            op = plan["operations"][0]
            if value == "missing":
                op.pop("subfolder", None)
            else:
                op["subfolder"] = None
            op["dest"] = op["dest"].replace("/PDF/", "/")
            plan["plan_id"] = f"{original['plan_id']}-{value}"
            plan["plan_hash"] = compute_plan_hash(plan)
            write_plan(settings, plan)
            with self.assertRaises(MoveAdmissionRefused):
                write_confirmation(settings, plan["plan_id"], plan["plan_hash"])
            with self.assertRaises(ExecutionRefused) as refused:
                execute_plan(settings, plan["plan_id"], plan["plan_hash"])
            self.assertIn("子类", str(refused.exception))
            self.assertTrue(Path(op["source"]).exists())

    def test_persisted_legacy_v2_completed_and_interrupted_records_roundtrip(self) -> None:
        """固定旧提交生成 v2 完成/中断记录，验证当前恢复不改历史。"""
        repo = Path(__file__).resolve().parents[1]
        def old_source(path: str) -> str:
            return fixture_bytes(self.LEGACY_FIXTURE_COMMIT, path).decode('utf-8')

        def load_old_module(name: str, source_path: str) -> types.ModuleType:
            module = types.ModuleType(f"doc_selecter._legacy_fixture_{name}")
            module.__package__ = "doc_selecter"
            sys.modules[module.__name__] = module
            exec(compile(old_source(source_path), f"{self.LEGACY_FIXTURE_COMMIT}:{source_path}", "exec"),
                 module.__dict__)
            return module

        old_version = load_old_module("version", "doc_selecter/__init__.py")
        old_hash = load_old_module("hash", "doc_selecter/plan_hash.py")
        old_planner = load_old_module("planner", "doc_selecter/planner.py")
        old_executor = load_old_module("executor", "doc_selecter/executor.py")
        old_planner.RULES_VERSION = old_version.RULES_VERSION
        old_planner.PLAN_SCHEMA_VERSION = old_hash.PLAN_SCHEMA_VERSION
        old_planner.PLAN_HASH_ALG = old_hash.PLAN_HASH_ALG
        old_planner.compute_plan_hash = old_hash.compute_plan_hash
        old_planner.verify_plan_hash = old_hash.verify_plan_hash
        for name in ("_MISSING", "load_verified_plan", "read_confirmation", "verify_plan_binding",
                     "directory_identity_key", "normalized_dir_identity_map", "MoveAdmissionRefused"):
            setattr(old_executor, name, getattr(old_planner, name))

        for case in ("completed", "interrupted"):
            sandbox = Sandbox(keep=False)
            sandbox.activate()
            try:
                payload = b"artificial legacy v2 payload"
                source = sandbox.add_file("legacy-report.pdf", payload)
                identity = lstat_identity(str(source)).as_dict()
                settings = sandbox.settings
                plan = old_planner.build_plan(settings).plan
                self.assertEqual(plan["rules_version"], "milestone4-privacy-r11")
                self.assertEqual(plan["schema_version"], 2)
                self.assertNotIn("document_subcategory", plan["operations"][0])
                op = plan["operations"][0]
                self.assertEqual(op["dest"], str(Path(settings.category_dir("文档资料")) / "legacy-report.pdf"))
                old_planner.write_plan(settings, plan)
                old_planner.write_confirmation(settings, plan["plan_id"], plan["plan_hash"])
                plan_path = Path(settings.plans_dir, f"{plan['plan_id']}.json")
                confirmation_path = Path(settings.confirmations_dir, f"{plan['plan_id']}.json")
                plan_bytes = plan_path.read_bytes()
                confirmation_bytes = confirmation_path.read_bytes()

                if case == "completed":
                    result = old_executor.execute_plan(settings, plan["plan_id"], plan["plan_hash"])
                    self.assertEqual(result.moved, 1)
                else:
                    journal = sandbox.journal(plan["plan_id"])
                    self.assertTrue(Path(op["source"]).is_file(), case)
                    journal.append(make_event(
                        "intent", op["op_id"], source=op["source"], dest=op["dest"],
                        category=op["category"], identity=op["identity"],
                        plan_hash=plan["plan_hash"],
                    ))
                    Path(op["dest"]).parent.mkdir(parents=True, exist_ok=True)
                    os.replace(op["source"], op["dest"])
                    self.assertIsNone(journal.fold()[0][op["op_id"]].get("result"))
                    recovered = recover_execution(settings, plan["plan_id"])
                    self.assertIn(op["op_id"], recovered["recovered"])

                undone = undo_plan(settings, plan["plan_id"], confirm=True)
                self.assertEqual(undone.restored, 1)
                self.assertEqual(plan_path.read_bytes(), plan_bytes)
                self.assertEqual(confirmation_path.read_bytes(), confirmation_bytes)
                self.assertEqual(lstat_identity(str(source)).as_dict(), identity)
                self.assertEqual(source.name, "legacy-report.pdf")
                self.assertEqual(source.read_bytes(), payload)
                self.assertFalse(os.path.lexists(op["dest"]))
                for kind in ("execution", "undo"):
                    self.assertEqual(sandbox.journal(plan["plan_id"], kind).classify().classification,
                                     "healthy")
            finally:
                sandbox.close()
if __name__ == "__main__":
    unittest.main()
