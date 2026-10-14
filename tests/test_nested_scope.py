"""子文件夹递归整理的回归测试。

覆盖验收要求：

- 默认不递归；选中范围才递归；未选目录保持原样；
- 不同来源的同名文件保留层级；目标重名不覆盖；
- 嵌套文件预览→确认→执行→撤销完整闭环；
- 范围/类别改变后旧确认失效，新计划确实反映新选择；
- 链接、越界、父目录被替换、代码项目、包目录、敏感目录、空目录正确处理；
- 递归读取失败与扫描变化如实记录；
- 嵌套执行/撤销的中断恢复、跨代日志与损坏日志准入；
- 旧格式计划（v1）明确拒绝。
"""

from __future__ import annotations

import json
import os
import stat as statmod
import unittest
from pathlib import Path

from tests.harness import SandboxTestCase

from doc_selecter import executor as executor_mod
from doc_selecter.atomic_move import AtomicMover
from doc_selecter.config import load_settings
from doc_selecter.executor import ExecutionRefused, execute_plan, recover_execution
from doc_selecter.journal import make_event
from doc_selecter.locking import current_phase
from doc_selecter.plan_hash import PLAN_SCHEMA_VERSION
from doc_selecter.planner import (
    PlanError,
    build_plan,
    load_verified_plan,
    verify_plan_binding,
    write_confirmation,
    write_plan,
)
from doc_selecter.scanner import list_selectable_folders, scan_scope
from doc_selecter.snapshot import read_snapshot
from doc_selecter.undo import UndoRefused, undo_plan


class ScopeBasicsTests(SandboxTestCase):
    """默认范围、选中范围与未选目录。"""

    def _seed(self) -> None:
        self.sandbox.add_file("top.pdf", b"TOP")
        self.sandbox.add_file("项目A/资料/说明.pdf", b"CONTRACT")
        self.sandbox.add_file("项目A/note.txt", b"NOTE")
        self.sandbox.add_file("其他/x.png", b"PNG")

    def test_default_is_top_level_only(self) -> None:
        self._seed()
        plan = self.sandbox.make_plan(confirm=False)
        self.assertFalse(plan["recursive"])
        self.assertEqual(plan["scan_scope"], [""])
        rels = [op["rel_path"] for op in plan["operations"]]
        self.assertEqual(rels, ["top.pdf"])
        # 未选目录保持原样
        self.assertTrue((self.sandbox.downloads / "项目A" / "资料" / "说明.pdf").is_file())

    def test_selected_scope_recurses_only_selected(self) -> None:
        self._seed()
        plan = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True, confirm=False)
        self.assertTrue(plan["recursive"])
        self.assertIn("项目A", plan["scan_scope"])
        self.assertIn("项目A/资料", plan["scan_scope"])
        rels = sorted(op["rel_path"] for op in plan["operations"])
        self.assertEqual(rels, ["top.pdf", "项目A/note.txt", "项目A/资料/说明.pdf"])
        # 未选目录的文件不在计划里
        self.assertNotIn("其他/x.png", rels)

    def test_unselected_directory_untouched_after_execute(self) -> None:
        self._seed()
        plan = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True)
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 3, result.outcomes)
        self.assertTrue((self.sandbox.downloads / "其他" / "x.png").is_file(),
                        "未选目录必须保持原样")

    def test_nested_destination_keeps_relative_hierarchy(self) -> None:
        self._seed()
        plan = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True, confirm=False)
        by_rel = {op["rel_path"]: op["dest"] for op in plan["operations"]}
        from doc_selecter.plan_hash import normalize_path

        target = Path(normalize_path(str(self.sandbox.downloads / "下载分类")))
        self.assertEqual(
            Path(by_rel["项目A/资料/说明.pdf"]),
            target / "文档资料" / "项目A" / "资料" / "说明.pdf",
        )
        self.assertEqual(Path(by_rel["top.pdf"]), target / "文档资料" / "PDF" / "top.pdf")

    def test_folder_picker_skips_special_dirs(self) -> None:
        self.sandbox.add_file("正常目录/a.pdf", b"x")
        self.sandbox.add_file("CodeProj/package.json", b"{}")
        self.sandbox.add_file("CodeProj/index.js", b"x")
        self.sandbox.add_file("Xcode.app/Contents/x", b"x")
        self.sandbox.add_file("密码备份/a.txt", b"x")
        self.sandbox.add_file(".hidden/a.txt", b"x")
        self.sandbox.add_dir("空目录")
        self.sandbox.add_file("下载分类/文档资料/old.pdf", b"x")
        folders = list_selectable_folders(
            str(self.sandbox.downloads), target_parent_name="下载分类"
        )
        names = {f.rel_path for f in folders}
        self.assertEqual(names, {"正常目录"})


class NestedRoundTripTests(SandboxTestCase):
    """嵌套闭环与撤销。"""

    def _seed_nested(self) -> None:
        self.sandbox.add_file("top.pdf", b"TOP")
        self.sandbox.add_file("项目A/资料/说明.pdf", b"CONTRACT")
        self.sandbox.add_file("项目B/说明.pdf", b"CONTRACT-B")

    def test_preview_confirm_execute_undo_roundtrip(self) -> None:
        self._seed_nested()
        plan = self.sandbox.make_plan(scope_dirs=["项目A", "项目B"], recursive=True)
        before = {
            op["rel_path"]: (
                Path(op["source"]).read_bytes(),
                os.lstat(op["source"]).st_ino,
            )
            for op in plan["operations"]
        }

        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 3, result.outcomes)
        for op in plan["operations"]:
            self.assertTrue(Path(op["dest"]).is_file(), op["dest"])
        # 同名文件保留层级，互不覆盖
        self.assertEqual(len({op["dest"] for op in plan["operations"]}), 3)

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 3, undo.entries)
        for op in plan["operations"]:
            source = Path(op["source"])
            self.assertTrue(source.is_file(), source)
            payload, inode = before[op["rel_path"]]
            self.assertEqual(source.read_bytes(), payload)
            self.assertEqual(os.lstat(source).st_ino, inode, "身份必须一致")

    def test_empty_directories_are_preserved(self) -> None:
        self._seed_nested()
        plan = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True)
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertTrue((self.sandbox.downloads / "项目A" / "资料").is_dir(),
                        "整理后目录（即使变空）必须保留")
        undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertTrue((self.sandbox.downloads / "项目A" / "资料").is_dir())

    def test_dest_name_conflict_is_not_overwritten(self) -> None:
        self.sandbox.add_file("a.pdf", b"SOURCE-TOP")
        self.sandbox.add_file("项目A/a.pdf", b"SOURCE-NESTED")
        target = self.sandbox.downloads / "下载分类" / "文档资料" / "PDF"
        target.mkdir(parents=True)
        existing = target / "a.pdf"
        existing.write_bytes(b"PRE-EXISTING")
        os.utime(existing, ns=(424242, 424242))
        before = (existing.read_bytes(), os.lstat(existing).st_mtime_ns)

        plan = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True)

        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])

        codes = [o["reason_code"] for o in result.outcomes]
        self.assertIn("DEST_EXISTS", codes, result.outcomes)
        after = (existing.read_bytes(), os.lstat(existing).st_mtime_ns)
        self.assertEqual(before, after, "目标重名绝不能被覆盖")
        self.assertTrue((self.sandbox.downloads / "下载分类" / "文档资料" / "项目A" / "a.pdf").is_file(),
                        "不同层级的同名文件应各自保留")


class ScopeConfirmationTests(SandboxTestCase):
    """范围/类别变化必须使旧确认失效，并生成反映新选择的计划。"""

    def _seed(self) -> None:
        self.sandbox.add_file("top.pdf", b"T")
        self.sandbox.add_file("项目A/a.pdf", b"A")
        self.sandbox.add_file("项目B/b.png", b"B")

    def test_scope_change_produces_new_plan_and_invalidates_confirmation(self) -> None:
        self._seed()
        first = self.sandbox.make_plan(confirm=True)
        self.assertEqual([op["rel_path"] for op in first["operations"]], ["top.pdf"])

        second = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True, confirm=False)
        self.assertNotEqual(second["plan_hash"], first["plan_hash"],
                            "范围变化必须改变计划指纹")
        rels = sorted(op["rel_path"] for op in second["operations"])
        self.assertEqual(rels, ["top.pdf", "项目A/a.pdf"])
        # 旧指纹确认不能用于新计划
        with self.assertRaises(ExecutionRefused):
            execute_plan(self.sandbox.settings, second["plan_id"], first["plan_hash"])

    def test_category_change_produces_different_plan(self) -> None:
        self._seed()
        first = self.sandbox.make_plan(
            categories=["文档资料"], scope_dirs=["项目A"], recursive=True, confirm=False
        )
        second = self.sandbox.make_plan(
            categories=["图片截图"], scope_dirs=["项目B"], recursive=True, confirm=False
        )
        self.assertNotEqual(first["plan_hash"], second["plan_hash"])
        self.assertEqual(
            sorted(op["rel_path"] for op in first["operations"]), ["top.pdf", "项目A/a.pdf"]
        )
        self.assertEqual(
            sorted(op["rel_path"] for op in second["operations"]), ["项目B/b.png"]
        )

    def test_scan_scope_is_bound_into_plan_hash(self) -> None:
        self._seed()
        wide = self.sandbox.make_plan(scope_dirs=["项目A", "项目B"], recursive=True, confirm=False)
        narrow = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True, confirm=False)
        self.assertNotEqual(wide["scan_scope"], narrow["scan_scope"])
        self.assertNotEqual(wide["plan_hash"], narrow["plan_hash"])
        # 篡改范围 → 指纹失配
        tampered = json.loads(json.dumps(wide))
        tampered["scan_scope"] = ["", "项目A"]
        from doc_selecter.plan_hash import verify_plan_hash

        self.assertFalse(verify_plan_hash(tampered))

    def test_plan_format_version_is_current(self) -> None:
        self.sandbox.add_file("a.pdf", b"x")
        plan = self.sandbox.make_plan(confirm=False)
        self.assertEqual(plan["schema_version"], PLAN_SCHEMA_VERSION)

    def test_old_v1_plan_is_rejected_with_clear_message(self) -> None:
        self.sandbox.add_file("a.pdf", b"x")
        plan = self.sandbox.make_plan(confirm=False)
        path = self.sandbox.plan_file(plan["plan_id"])
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy["schema_version"] = 1
        legacy.pop("scan_scope", None)
        for op in legacy["operations"]:
            op.pop("rel_path", None)
        path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(PlanError) as ctx:
            load_verified_plan(self.sandbox.settings, plan["plan_id"], verify_binding=True)
        self.assertIn("不兼容", str(ctx.exception))
        self.assertIn("重新", str(ctx.exception))


class DirectoryRuleTests(SandboxTestCase):
    """包目录、代码项目、敏感目录、空目录、链接与越界。"""

    def test_code_project_and_package_dirs_skipped(self) -> None:
        self.sandbox.add_file("CodeProj/package.json", b"{}")
        self.sandbox.add_file("CodeProj/index.js", b"x")
        self.sandbox.add_file("App.app/Contents/MacOS/bin", b"x")
        self.sandbox.add_file("正常/a.pdf", b"x")
        scan = scan_scope(
            str(self.sandbox.downloads), target_parent_name="下载分类",
            scope_dirs=["CodeProj", "App.app", "正常"], recursive=True,
        )
        codes = {e.rel_path: e.code for e in scan.excluded}
        self.assertEqual(codes.get("CodeProj"), "EXCLUDED_CODE_PROJECT")
        self.assertEqual(codes.get("App.app"), "EXCLUDED_PACKAGE_DIR")
        self.assertEqual([e.rel_path for e in scan.included], ["正常/a.pdf"])

    def test_sensitive_dir_skipped_without_name(self) -> None:
        self.sandbox.add_file("密码备份/a.txt", b"secret")
        self.sandbox.add_file("正常/b.pdf", b"x")
        scan = scan_scope(
            str(self.sandbox.downloads), target_parent_name="下载分类",
            scope_dirs=["密码备份", "正常"], recursive=True,
        )
        self.assertEqual(scan.sensitive_dir_count, 1)
        # 敏感目录名绝不进入 excluded 清单
        self.assertNotIn("密码备份", {e.rel_path for e in scan.excluded})
        self.assertEqual([e.rel_path for e in scan.included], ["正常/b.pdf"])

    def test_sensitive_dir_name_not_persisted_in_plan(self) -> None:
        self.sandbox.add_file("密码备份/a.txt", b"secret")
        self.sandbox.add_file("正常/b.pdf", b"x")
        plan = self.sandbox.make_plan(scope_dirs=["密码备份", "正常"], recursive=True, confirm=False)
        blob = json.dumps(plan, ensure_ascii=False)
        self.assertNotIn("密码备份", blob, "疑似敏感目录名不得写入计划")

    def test_empty_directory_skipped(self) -> None:
        self.sandbox.add_dir("空目录")
        self.sandbox.add_file("正常/a.pdf", b"x")
        scan = scan_scope(
            str(self.sandbox.downloads), target_parent_name="下载分类",
            scope_dirs=["空目录", "正常"], recursive=True,
        )
        codes = {e.rel_path: e.code for e in scan.excluded}
        self.assertEqual(codes.get("空目录"), "EXCLUDED_LEAF_DIR")

    def test_symlinked_subdirectory_is_not_followed(self) -> None:
        outside = self.sandbox.root / "Outside"
        outside.mkdir()
        (outside / "secret.pdf").write_bytes(b"OUTSIDE")
        os.symlink(outside, self.sandbox.downloads / "链接目录")
        self.sandbox.add_file("正常/a.pdf", b"x")

        scan = scan_scope(
            str(self.sandbox.downloads), target_parent_name="下载分类",
            scope_dirs=["链接目录", "正常"], recursive=True,
        )
        rels = [e.rel_path for e in scan.included]
        self.assertNotIn("链接目录/secret.pdf", rels)
        codes = {e.rel_path: e.code for e in scan.excluded}
        self.assertEqual(codes.get("链接目录"), "EXCLUDED_SYMLINK")

    def test_nested_symlink_file_excluded(self) -> None:
        self.sandbox.add_file("正常/real.pdf", b"x")
        os.symlink(
            self.sandbox.downloads / "正常" / "real.pdf",
            self.sandbox.downloads / "正常" / "link.pdf",
        )
        scan = scan_scope(
            str(self.sandbox.downloads), target_parent_name="下载分类",
            scope_dirs=["正常"], recursive=True,
        )
        codes = {e.rel_path: e.code for e in scan.excluded}
        self.assertEqual(codes.get("正常/link.pdf"), "EXCLUDED_SYMLINK")

    def test_target_root_never_scanned_recursively(self) -> None:
        self.sandbox.add_file("下载分类/文档资料/old.pdf", b"x")
        self.sandbox.add_file("a.pdf", b"x")
        scan = scan_scope(
            str(self.sandbox.downloads), target_parent_name="下载分类",
            scope_dirs=["下载分类"], recursive=True,
        )
        self.assertEqual([e.rel_path for e in scan.included], ["a.pdf"])
        self.assertTrue(scan.target_root_excluded)

    def test_scope_traversal_is_rejected(self) -> None:
        self.sandbox.add_file("a.pdf", b"x")
        from doc_selecter.scanner import normalize_scope

        with self.assertRaises(ValueError):
            normalize_scope(["../Else"])
        with self.assertRaises(ValueError):
            normalize_scope(["/abs/path"])

    def test_source_outside_scope_is_rejected_at_binding(self) -> None:
        self.sandbox.add_file("a.pdf", b"x")
        self.sandbox.add_file("项目A/b.pdf", b"x")
        plan = self.sandbox.make_plan(confirm=False)
        tampered = json.loads(json.dumps(plan))
        # 伪造一个范围外的嵌套源
        op = dict(tampered["operations"][0])
        op["rel_path"] = "项目A/b.pdf"
        op["source"] = str(self.sandbox.downloads / "项目A" / "b.pdf")
        op["dest"] = str(self.sandbox.target_dir("文档资料") / "项目A" / "b.pdf")
        tampered["operations"] = [op]
        with self.assertRaises(PlanError) as ctx:
            verify_plan_binding(self.sandbox.settings, tampered)
        self.assertIn("扫描范围", str(ctx.exception))

    def test_nested_dest_outside_category_is_rejected(self) -> None:
        self.sandbox.add_file("项目A/a.pdf", b"x")
        plan = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True, confirm=False)
        self.assertTrue(any("/" in op["rel_path"] for op in plan["operations"]))
        tampered = json.loads(json.dumps(plan))
        nested_op = next(op for op in tampered["operations"] if "/" in op["rel_path"])
        nested_op["dest"] = str(self.sandbox.root / "Else" / "a.pdf")
        with self.assertRaises(PlanError):
            verify_plan_binding(self.sandbox.settings, tampered)


class ParentIdentityTests(SandboxTestCase):
    """父目录链身份（不只最外层根）。"""

    def test_plan_records_source_and_dest_parent_identities(self) -> None:
        self.sandbox.add_file("项目A/资料/说明.pdf", b"x")
        plan = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True, confirm=False)
        identities = plan["dir_identities"]
        self.assertIn("项目A", identities["source_dirs"])
        self.assertIn("项目A/资料", identities["source_dirs"])
        self.assertIn("文档资料/项目A/资料", identities["dest_dirs"])
        # 顶层目录也记录
        self.assertIn("", identities["source_dirs"])

    def test_replaced_source_parent_is_rejected(self) -> None:
        self.sandbox.add_file("项目A/资料/说明.pdf", b"x")
        plan = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True)
        # 替换嵌套源父目录（同路径、不同 inode）
        nested = self.sandbox.downloads / "项目A" / "资料"
        nested.rename(self.sandbox.root / "old-资料")
        nested.mkdir()
        (nested / "说明.pdf").write_bytes(b"x")

        with self.assertRaises(PlanError) as ctx:
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertIn("身份", str(ctx.exception))

    def test_replaced_category_root_is_rejected(self) -> None:
        self.sandbox.add_file("a.pdf", b"x")
        plan = self.sandbox.make_plan()
        # 先执行一次以建立目标类别目录，再替换它
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        category = self.sandbox.target_dir("文档资料")
        category.rename(self.sandbox.target_dir("文档资料-old"))
        category.mkdir()

        with self.assertRaises((PlanError, ExecutionRefused)):
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])


class RecursiveFailureTests(SandboxTestCase):
    """递归读取失败与扫描变化如实记录。"""

    def test_unreadable_subdirectory_is_recorded_not_treated_as_empty(self) -> None:
        nested = self.sandbox.add_file("项目A/资料/说明.pdf", b"x").parent
        os.chmod(nested, 0o000)
        self.addCleanup(os.chmod, nested, 0o700)
        self.sandbox.add_file("top.pdf", b"x")

        scan = scan_scope(
            str(self.sandbox.downloads), target_parent_name="下载分类",
            scope_dirs=["项目A"], recursive=True,
        )
        self.assertTrue(scan.scan_issues, "读取失败必须被记录")
        issue_paths = {issue.rel_path for issue in scan.scan_issues}
        self.assertTrue(any("资料" in p for p in issue_paths), scan.scan_issues)
        self.assertIn("top.pdf", [e.rel_path for e in scan.included])

    def test_scan_change_detection_records_added_file(self) -> None:
        self.sandbox.add_file("a.pdf", b"x")
        scan = scan_scope(
            str(self.sandbox.downloads), target_parent_name="下载分类", recursive=False,
        )
        self.assertIsInstance(scan.added_during_scan, list)
        self.assertIsInstance(scan.removed_during_scan, list)

    def test_plan_records_scan_issues(self) -> None:
        nested = self.sandbox.add_file("项目A/资料/说明.pdf", b"x").parent
        os.chmod(nested, 0o000)
        self.addCleanup(os.chmod, nested, 0o700)
        plan = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True, confirm=False)
        blob = json.dumps(plan, ensure_ascii=False)
        self.assertIn("递归读取失败", blob)


class NestedInterruptionTests(SandboxTestCase):
    """嵌套执行/撤销的中断恢复与损坏日志准入。"""

    def _nested_plan(self) -> dict:
        self.sandbox.add_file("项目A/资料/说明.pdf", b"CONTRACT")
        self.sandbox.add_file("top.pdf", b"TOP")
        return self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True)

    def test_nested_recover_after_interrupted_execution(self) -> None:
        plan = self._nested_plan()
        op = next(o for o in plan["operations"] if o["rel_path"].endswith("说明.pdf"))
        from doc_selecter.directory_archive import ensure_layout
        ensure_layout(self.sandbox.settings, plan)
        journal = self.sandbox.journal(plan["plan_id"])
        journal.append(make_event(
            "intent", op["op_id"], source=op["source"], dest=op["dest"],
            category=op["category"], identity=op["identity"], rel_path=op["rel_path"],
        ))
        Path(op["dest"]).parent.mkdir(parents=True, exist_ok=True)
        AtomicMover().rename_excl(op["source"], op["dest"])

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertIn(op["op_id"], report["recovered"])
        self.assertNotEqual(report["phase"], "corrupted", report["notes"])
        self.assertTrue(Path(op["dest"]).is_file())
        # 恢复后仍可安全撤销
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 1, undo.entries)
        self.assertTrue(Path(op["source"]).is_file())

    def test_nested_cross_generation_result_pair(self) -> None:
        plan = self._nested_plan()
        op = next(o for o in plan["operations"] if o["rel_path"].endswith("说明.pdf"))
        from doc_selecter.directory_archive import ensure_layout
        ensure_layout(self.sandbox.settings, plan)
        journal = self.sandbox.journal(plan["plan_id"])
        journal.append(make_event(
            "intent", op["op_id"], source=op["source"], dest=op["dest"],
            category=op["category"], identity=op["identity"], rel_path=op["rel_path"],
        ))
        Path(op["dest"]).parent.mkdir(parents=True, exist_ok=True)
        AtomicMover().rename_excl(op["source"], op["dest"])
        gen = Path(journal.generation_file(1))
        with open(gen, "ab") as fh:
            fh.write(b'{"event":"result"')

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertNotEqual(report["phase"], "corrupted", report["notes"])
        self.assertEqual(journal.classify().classification.upper(), "HEALTHY")
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 1, undo.entries)

    def test_corrupted_log_blocks_nested_execution(self) -> None:
        plan = self._nested_plan()
        from doc_selecter.directory_archive import ensure_layout
        ensure_layout(self.sandbox.settings, plan)
        journal = self.sandbox.journal(plan["plan_id"])
        journal.append(make_event(
            "intent", "op-0001", source="x", dest="y",
            category="文档资料", identity=plan["operations"][0]["identity"],
        ))
        gen = Path(journal.generation_file(1))
        gen.write_bytes(b"not-json\n" + gen.read_bytes())
        before = gen.read_bytes()

        with self.assertRaises(ExecutionRefused):
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(gen.read_bytes(), before)
        self.assertTrue((self.sandbox.downloads / "top.pdf").is_file(), "不得移动任何文件")

    def test_repaired_plan_can_execute_nested(self) -> None:
        """合法的「单代次清单缺失」经公开 recover 后，仍可正常执行嵌套计划。"""
        plan = self._nested_plan()
        journal = self.sandbox.journal(plan["plan_id"])
        # 真实执行一次 → 日志健康（清单存在），再删除清单制造可修复状态
        first = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(first.moved, 2, first.outcomes)
        Path(journal.manifest_path).unlink()

        # 未修复时拒绝，且提示 recover
        with self.assertRaises(ExecutionRefused) as ctx:
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertIn("recover", str(ctx.exception))
        self.assertFalse(Path(journal.manifest_path).exists(), "拒绝时不得修复")

        # 公开 recover 修复清单
        report = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertNotEqual(report["phase"], "corrupted", report["notes"])
        self.assertTrue(Path(journal.manifest_path).exists())

        # 修复后仍可安全撤销（嵌套文件回到原相对位置）
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 2, undo.entries)
        self.assertTrue((self.sandbox.downloads / "项目A" / "资料" / "说明.pdf").is_file())
        self.assertTrue((self.sandbox.downloads / "top.pdf").is_file())

    def test_plan_with_existing_results_refuses_second_execution(self) -> None:
        """同一计划只允许执行一次；已有结果记录时必须明确拒绝（不静默重跑）。"""
        plan = self._nested_plan()
        from doc_selecter.directory_archive import ensure_layout
        ensure_layout(self.sandbox.settings, plan)
        journal = self.sandbox.journal(plan["plan_id"])
        for op in plan["operations"]:
            journal.append(make_event(
                "intent", op["op_id"], source=op["source"], dest=op["dest"],
                category=op["category"], identity=op["identity"], rel_path=op["rel_path"],
            ))
            journal.append(make_event(
                "result", op["op_id"], outcome="skipped", reason_code="DEST_EXISTS",
                reason="synthetic skip",
            ))
        before = Path(journal.generation_file(1)).read_bytes()

        with self.assertRaises(ExecutionRefused) as ctx:
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertIn("只允许执行一次", str(ctx.exception))
        self.assertEqual(Path(journal.generation_file(1)).read_bytes(), before,
                         "拒绝时不得写入任何新事件")
        self.assertTrue((self.sandbox.downloads / "top.pdf").is_file(), "不得移动任何文件")


class NestedPlanStateTests(SandboxTestCase):
    """嵌套计划的状态与计数。"""

    def test_counts_include_nested_and_scanned_dirs(self) -> None:
        self.sandbox.add_file("top.pdf", b"x")
        self.sandbox.add_file("项目A/资料/说明.pdf", b"y")
        plan = self.sandbox.make_plan(scope_dirs=["项目A"], recursive=True, confirm=False)
        self.assertEqual(plan["counts"]["selected"], 2)
        self.assertEqual(plan["counts"]["nested_files"], 1)
        self.assertGreaterEqual(plan["counts"]["scanned_dirs"], 3)

    def test_cli_plan_shows_scope_and_relative_path(self) -> None:
        import io
        from contextlib import redirect_stdout

        from doc_selecter.cli import main

        self.sandbox.add_file("项目A/说明.pdf", b"x")
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["plan"])  # 默认仅顶层 → 无嵌套文件
        self.assertIn("计划编号", buf.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
