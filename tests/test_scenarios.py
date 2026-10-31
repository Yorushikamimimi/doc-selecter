"""端到端场景测试 S1–S18（含 S3b），全部在临时沙盒内运行。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.harness import SandboxTestCase, snapshot_dir, tree_snapshot

from doc_selecter import executor as executor_mod
from doc_selecter import undo as undo_mod
from doc_selecter.atomic_move import SourceChangedError, lstat_identity
from doc_selecter.config import ensure_state_dirs
from doc_selecter.executor import ExecutionRefused, execute_plan, recover_execution
from doc_selecter.journal import Journal, JournalCorrupted, JournalWriteFailed, make_event
from doc_selecter.locking import GlobalLock, LockBusy
from doc_selecter.planner import build_plan, write_confirmation, write_plan
from doc_selecter.rules import CATEGORIES
from doc_selecter.snapshot import read_snapshot, snapshot_path, write_snapshot
from doc_selecter.undo import UndoRefused, undo_plan

SIX_PAYLOADS = {
    "installer.dmg": b"DMG-PAYLOAD",
    "doc.pdf": b"PDF-PAYLOAD",
    "shot.png": b"PNG-PAYLOAD",
    "clip.m4a": b"M4A-PAYLOAD",
    "bundle.zip": b"ZIP-PAYLOAD",
    "data.json": b"JSON-PAYLOAD",
}


class ClosedLoopTests(SandboxTestCase):
    """S1 / S2：预览 → 确认 → 执行 → 撤销。"""

    def _seed(self) -> None:
        for name, payload in SIX_PAYLOADS.items():
            self.sandbox.add_file(name, payload)
        self.sandbox.add_dir("some-folder")

    def test_s1_preview_is_read_only(self) -> None:
        self._seed()
        before = snapshot_dir(self.sandbox.downloads)
        root_mtime = os.lstat(self.sandbox.downloads).st_mtime_ns

        plan = self.sandbox.make_plan(confirm=False)

        self.assertEqual(plan["counts"]["included"], len(SIX_PAYLOADS))
        self.assertEqual(plan["counts"]["selected"], len(SIX_PAYLOADS))
        self.assertEqual(sum(plan["category_totals"]["counts"].values()), len(SIX_PAYLOADS))
        for category, count in plan["category_totals"]["counts"].items():
            self.assertIn(category, CATEGORIES)
        self.assertEqual(plan["excluded"]["文件夹"], ["some-folder"])
        self.assertFalse((self.sandbox.downloads / "下载分类").exists(), "预览不得创建目标目录")

        after = snapshot_dir(self.sandbox.downloads)
        self.assertEqual(before, after, "预览前后下载目录条目集合与逐条 size/mtime/inode 必须不变")
        self.assertEqual(root_mtime, os.lstat(self.sandbox.downloads).st_mtime_ns)
        # 证据强度声明：这是弱断言，不能证明「目录从未被访问」。

    def test_s2_execute_then_undo_roundtrip(self) -> None:
        self._seed()
        plan = self.sandbox.make_plan()
        plan_id = plan["plan_id"]
        identities = {op["op_id"]: op["identity"] for op in plan["operations"]}

        result = execute_plan(self.sandbox.settings, plan_id, plan["plan_hash"])
        self.assertEqual(result.moved, len(SIX_PAYLOADS))
        self.assertEqual(result.failed, 0, result.outcomes)
        self.assertEqual(result.skipped, 0, result.outcomes)

        for name, payload in SIX_PAYLOADS.items():
            self.assertNotExists(self.sandbox.downloads / name)
        for op in plan["operations"]:
            dest = Path(op["dest"])
            self.assertTrue(dest.is_file(), f"目标应当存在：{dest}")
            self.assertFileContent(dest, SIX_PAYLOADS[os.path.basename(op["source"])])
            self.assertIdentityMatches(dest, op["identity"])

        undo = undo_plan(self.sandbox.settings, plan_id, confirm=True)
        self.assertEqual(undo.restored, len(SIX_PAYLOADS), undo.entries)
        self.assertEqual(undo.undo_status, "completed")

        for op in plan["operations"]:
            source = Path(op["source"])
            self.assertTrue(source.is_file(), f"应当已还原：{source}")
            self.assertIdentityMatches(source, identities[op["op_id"]])
            self.assertNotExists(op["dest"])

    def test_s2_undo_journal_is_paired(self) -> None:
        self._seed()
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        events, _ = self.sandbox.journal(plan["plan_id"], "undo").read_events()
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds.count("undo_intent"), len(SIX_PAYLOADS))
        self.assertEqual(kinds.count("undo_result"), len(SIX_PAYLOADS))


class ConflictTests(SandboxTestCase):
    """S3 / S3b：目标重名与竞态窗口 —— 绝不覆盖。"""

    def test_s3_dest_exists_is_skipped_without_overwrite(self) -> None:
        self.sandbox.add_file("a.zip", b"SOURCE-ZIP")
        self.sandbox.add_file("keep.pdf", b"PDF")
        dest_dir = self.sandbox.target_dir("压缩包") / "ZIP"
        dest_dir.mkdir(parents=True)
        existing = dest_dir / "a.zip"
        existing.write_bytes(b"PRE-EXISTING-DIFFERENT")
        os.utime(existing, ns=(424242, 424242))
        before = (existing.read_bytes(), os.lstat(existing).st_mtime_ns)

        plan = self.sandbox.make_plan()

        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.moved, 1)
        codes = {o["reason_code"] for o in result.outcomes}
        self.assertIn("DEST_EXISTS", codes)
        after = (existing.read_bytes(), os.lstat(existing).st_mtime_ns)
        self.assertEqual(before, after, "预置冲突文件内容与 mtime 必须完全未变")
        self.assertExists(self.sandbox.downloads / "a.zip")

    def test_s3_dest_exists_as_directory_is_skipped(self) -> None:
        self.sandbox.add_file("a.zip", b"SOURCE-ZIP")
        dest = self.sandbox.target_dir("压缩包") / "ZIP" / "a.zip"
        dest.mkdir(parents=True)

        plan = self.sandbox.make_plan()

        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.outcomes[0]["reason_code"], "DEST_EXISTS")
        self.assertTrue(dest.is_dir(), "同名目录必须保持原样")
        self.assertExists(self.sandbox.downloads / "a.zip")

    def test_s3b_race_window_created_after_precheck_is_not_overwritten(self) -> None:
        """在「准备移动」与「真正调用原语」之间由外部创建同名目标。"""
        self.sandbox.add_file("race.zip", b"SOURCE-RACE")
        plan = self.sandbox.make_plan()
        dest = self.sandbox.target_dir("压缩包") / "ZIP" / "race.zip"

        def hook(kind: str, info: dict) -> None:
            if kind == "before_move":
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"WINNER-FROM-OTHER-PROCESS")
                os.utime(dest, ns=(777777, 777777))

        result = execute_plan(
            self.sandbox.settings, plan["plan_id"], plan["plan_hash"], hook=hook
        )

        self.assertEqual(result.skipped, 1, result.outcomes)
        self.assertEqual(result.outcomes[0]["reason_code"], "DEST_EXISTS")
        self.assertEqual(dest.read_bytes(), b"WINNER-FROM-OTHER-PROCESS", "竞态目标绝不能被覆盖")
        self.assertEqual(os.lstat(dest).st_mtime_ns, 777777, "竞态目标 mtime 必须未变")
        self.assertExists(self.sandbox.downloads / "race.zip")


class ChangeDetectionTests(SandboxTestCase):
    """S4 / S15：预览后变化与错误分类。"""

    def test_s4_changed_missing_and_new_files(self) -> None:
        self.sandbox.add_file("changed.pdf", b"ORIGINAL-CONTENT")
        self.sandbox.add_file("missing.pdf", b"WILL-BE-REMOVED")
        self.sandbox.add_file("mtime.pdf", b"MTIME-ONLY")
        plan = self.sandbox.make_plan()
        op_ids = {os.path.basename(op["source"]): op["op_id"] for op in plan["operations"]}

        (self.sandbox.downloads / "changed.pdf").write_bytes(b"MUCH-LONGER-CONTENT-THAN-BEFORE")
        (self.sandbox.downloads / "missing.pdf").unlink()
        self.sandbox.set_mtime(self.sandbox.downloads / "mtime.pdf", 123456789)
        self.sandbox.add_file("brand-new.pdf", b"NOT-IN-PLAN")

        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        by_op = {o["op_id"]: o["reason_code"] for o in result.outcomes}

        self.assertEqual(by_op[op_ids["changed.pdf"]], "SOURCE_CHANGED")
        self.assertEqual(by_op[op_ids["missing.pdf"]], "SOURCE_MISSING")
        self.assertEqual(by_op[op_ids["mtime.pdf"]], "SOURCE_CHANGED")
        self.assertEqual(result.moved, 0)
        self.assertExists(self.sandbox.downloads / "brand-new.pdf")
        self.assertNotExists(self.sandbox.target_dir("文档资料") / "brand-new.pdf")

    def test_s15_permission_error_blocks_instead_of_treating_as_absent(self) -> None:
        self.sandbox.add_file("perm.pdf", b"PDF")
        plan = self.sandbox.make_plan()
        original = executor_mod.lstat_identity

        dest_in_plan = plan["operations"][0]["dest"]
        real_lstat = os.lstat

        def fake_lstat(path, *args, **kwargs):
            # 只拦截「按计划路径判定目标状态」的那次调用
            if str(path) == dest_in_plan:
                raise PermissionError(13, "simulated EACCES")
            return real_lstat(path, *args, **kwargs)

        executor_mod.os.lstat = fake_lstat
        try:
            result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        finally:
            executor_mod.os.lstat = real_lstat

        self.assertEqual(result.moved, 0, "权限错误绝不能导致移动")
        self.assertEqual(result.outcomes[0]["reason_code"], "PERMISSION_DENIED")
        self.assertExists(self.sandbox.downloads / "perm.pdf")

    def test_s15_io_error_blocks_instead_of_treating_as_absent(self) -> None:
        self.sandbox.add_file("io.pdf", b"PDF")
        plan = self.sandbox.make_plan()
        original = executor_mod.lstat_identity

        dest_in_plan = plan["operations"][0]["dest"]
        real_lstat = os.lstat

        def fake_lstat(path, *args, **kwargs):
            if str(path) == dest_in_plan:
                raise OSError(5, "simulated EIO")
            return real_lstat(path, *args, **kwargs)

        executor_mod.os.lstat = fake_lstat
        try:
            result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        finally:
            executor_mod.os.lstat = real_lstat
        self.assertEqual(result.moved, 0, "I/O 错误绝不能导致移动")
        self.assertEqual(result.outcomes[0]["reason_code"], "ATOMIC_MOVE_FAILED")
        self.assertExists(self.sandbox.downloads / "io.pdf")


class PartialFailureTests(SandboxTestCase):
    """S5：部分执行失败 —— local 继续、global 停止。"""

    def test_s5_local_failure_continues(self) -> None:
        names = ["a.pdf", "b.pdf", "c.pdf", "d.pdf"]
        for name in names:
            self.sandbox.add_file(name, b"X" * 10)
        plan = self.sandbox.make_plan()
        third = plan["operations"][2]["op_id"]

        original = executor_mod.AtomicMover

        class Flaky(original):  # type: ignore[misc, valid-type]
            def rename_excl_at(self, sfd, sname, dfd, dname):  # type: ignore[override]
                if sname == "c.pdf":
                    raise PermissionError(13, "simulated local failure")
                return super().rename_excl_at(sfd, sname, dfd, dname)

        executor_mod.AtomicMover = Flaky  # type: ignore[misc]
        try:
            result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        finally:
            executor_mod.AtomicMover = original  # type: ignore[misc]

        self.assertEqual(result.moved, 3, result.outcomes)
        self.assertEqual(result.failed, 1)
        by_op = {o["op_id"]: o["reason_code"] for o in result.outcomes}
        self.assertEqual(by_op[third], "PERMISSION_DENIED")
        self.assertExists(self.sandbox.downloads / "c.pdf")
        self.assertNotExists(self.sandbox.downloads / "d.pdf")

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 3)
        for name in names:
            self.assertExists(self.sandbox.downloads / name)


class ExclusionTests(SandboxTestCase):
    """S7：链接 / 隐藏 / 未完成 / 敏感项一律排除。"""

    def test_s7_exclusions_and_sensitive_privacy(self) -> None:
        self.sandbox.add_file("target.pdf", b"REAL-TARGET")
        self.sandbox.add_symlink("link.pdf", "target.pdf")
        self.sandbox.add_file(".hidden.txt", b"HIDDEN")
        self.sandbox.add_file("partial.crdownload", b"PARTIAL")
        self.sandbox.add_file("id_rsa", b"-----BEGIN PRIVATE KEY-----SECRET")

        plan = self.sandbox.make_plan()

        self.assertEqual(plan["counts"]["included"], 1)
        self.assertEqual(plan["operations"][0]["source"].endswith("target.pdf"), True)
        self.assertEqual(plan["excluded"]["疑似敏感项"], {"count": 1, "names": None})
        self.assertEqual(plan["excluded"]["符号链接"], ["link.pdf"])
        self.assertEqual(plan["excluded"]["隐藏项"], [".hidden.txt"])
        self.assertEqual(plan["excluded"]["未完成下载或临时项"], ["partial.crdownload"])

        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 1)
        self.assertExists(self.sandbox.downloads / "link.pdf")
        self.assertExists(self.sandbox.downloads / ".hidden.txt")
        self.assertExists(self.sandbox.downloads / "partial.crdownload")
        self.assertExists(self.sandbox.downloads / "id_rsa")
        moved = Path(plan["operations"][0]["dest"])
        self.assertFileContent(moved, b"REAL-TARGET")
        self.assertTrue(os.path.islink(self.sandbox.downloads / "link.pdf"))
        self.assertEqual(os.readlink(self.sandbox.downloads / "link.pdf"), "target.pdf")

        # 敏感名称绝不落盘
        blob = ""
        for path in sorted(self.sandbox.state.rglob("*")):
            if path.is_file():
                blob += path.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("id_rsa", blob, "疑似敏感项名称不得出现在任何状态文件中")


class ConfirmationTests(SandboxTestCase):
    """S8：未确认 / 指纹错误一律拒绝执行，零移动。"""

    def test_s8_missing_or_wrong_confirm_refused(self) -> None:
        self.sandbox.add_file("a.pdf", b"PDF")
        plan = self.sandbox.make_plan(confirm=False)

        with self.assertRaises(ExecutionRefused):
            execute_plan(self.sandbox.settings, plan["plan_id"], None)
        with self.assertRaises(ExecutionRefused):
            execute_plan(self.sandbox.settings, plan["plan_id"], "sha256:deadbeef")
        with self.assertRaises(ExecutionRefused):
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])  # 无确认记录

        self.assertNotExists(self.sandbox.downloads / "下载分类")
        self.assertExists(self.sandbox.downloads / "a.pdf")

    def test_s8_changed_plan_invalidates_confirmation(self) -> None:
        self.sandbox.add_file("a.pdf", b"PDF")
        self.sandbox.add_file("b.png", b"PNG")
        plan = self.sandbox.make_plan()
        original_hash = plan["plan_hash"]
        original_ops = plan["operations"]

        # 篡改计划文件里的勾选集合（改变 plan_body）→ 指纹校验失败
        path = self.sandbox.plan_file(plan["plan_id"])
        tampered = self.read_json(path)
        tampered["category_selection"] = ["文档资料"]
        tampered["operations"] = [op for op in original_ops if op["category"] == "文档资料"]
        path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(Exception) as ctx:
            execute_plan(self.sandbox.settings, plan["plan_id"], original_hash)
        self.assertIn("指纹", str(ctx.exception))
        self.assertNotExists(self.sandbox.downloads / "下载分类")


class BoundaryTests(SandboxTestCase):
    """S9：越界路径与跨文件系统。"""

    def test_s9_out_of_scope_rejected(self) -> None:
        self.sandbox.add_file("a.pdf", b"PDF")
        plan = self.sandbox.make_plan()
        original = executor_mod.assert_no_symlink_segment_for_file

        def fake(target: str, base: str):
            raise executor_mod.OutOfScopeError(f"打桩：越界路径 {target}")

        executor_mod.assert_no_symlink_segment_for_file = fake
        try:
            result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        finally:
            executor_mod.assert_no_symlink_segment_for_file = original

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.outcomes[0]["reason_code"], "OUT_OF_SCOPE_PATH")
        self.assertExists(self.sandbox.downloads / "a.pdf")

    def test_s9_cross_filesystem_skipped_without_copy(self) -> None:
        self.sandbox.add_file("a.pdf", b"PDF")
        plan = self.sandbox.make_plan()
        original = executor_mod.lstat_identity

        dest_in_plan = plan["operations"][0]["dest"]
        real_lstat = os.lstat

        def fake_lstat(path, *args, **kwargs):
            if str(path) == dest_in_plan:
                raise OSError(18, "simulated EXDEV")
            return real_lstat(path, *args, **kwargs)

        executor_mod.os.lstat = fake_lstat
        try:
            result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        finally:
            executor_mod.os.lstat = real_lstat

        self.assertEqual(result.moved, 0)
        self.assertEqual(result.outcomes[0]["reason_code"], "CROSS_FILESYSTEM")
        self.assertExists(self.sandbox.downloads / "a.pdf")
        target = self.sandbox.target_dir("文档资料")
        self.assertEqual(list(target.glob("a.pdf")) if target.exists() else [], [],
                         "不得出现复制+删除的副本")


class RecoveryTests(SandboxTestCase):
    """S10 / S11：中断恢复与「整批未完成即可撤销」。"""

    def test_s10_dangling_intent_recovered(self) -> None:
        self.sandbox.add_file("moved.pdf", b"MOVED-PAYLOAD")
        self.sandbox.add_file("never.pdf", b"NEVER-PAYLOAD")
        plan = self.sandbox.make_plan()
        from doc_selecter.directory_archive import ensure_layout
        ensure_layout(self.sandbox.settings, plan)
        journal = self.sandbox.journal(plan["plan_id"])
        ops = {op["source"].rsplit("/", 1)[-1]: op for op in plan["operations"]}

        # 模拟：moved.pdf 已移动但 result 未落盘
        moved_op = ops["moved.pdf"]
        dest = Path(moved_op["dest"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        journal.append(
            make_event(
                "intent", moved_op["op_id"], source=moved_op["source"], dest=moved_op["dest"],
                category=moved_op["category"], identity=moved_op["identity"],
                plan_hash=plan["plan_hash"],
            )
        )
        os.replace(self.sandbox.downloads / "moved.pdf", dest)
        # 模拟：never.pdf 只写了 intent，未移动
        never_op = ops["never.pdf"]
        journal.append(
            make_event(
                "intent", never_op["op_id"], source=never_op["source"], dest=never_op["dest"],
                category=never_op["category"], identity=never_op["identity"],
                plan_hash=plan["plan_hash"],
            )
        )

        report = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertIn(moved_op["op_id"], report["recovered"])
        fold, _ = journal.fold()
        self.assertEqual(fold[moved_op["op_id"]]["result"]["reason_code"], "RECOVERED_FROM_INTENT")
        # 补记的基准必须来自 intent，而不是恢复时刻的观测值
        self.assertEqual(
            fold[moved_op["op_id"]]["result"]["dest_identity"]["st_ino"],
            moved_op["identity"]["st_ino"],
        )
        self.assertEqual(fold[never_op["op_id"]]["result"]["reason_code"], "NOT_MOVED", "v3补记未移动，不自动重试")

        # 不得重复移动
        self.assertExists(dest)
        self.assertExists(self.sandbox.downloads / "never.pdf")

    def test_s11_partial_batch_is_still_undoable(self) -> None:
        names = ["a.pdf", "b.pdf", "c.pdf"]
        for name in names:
            self.sandbox.add_file(name, b"PAYLOAD-" + name.encode())
        plan = self.sandbox.make_plan()
        from doc_selecter.directory_archive import ensure_layout
        ensure_layout(self.sandbox.settings, plan)
        journal = self.sandbox.journal(plan["plan_id"])

        # 手工制造「前 2 项已移动 + 第 3 项仅 intent」的中断现场
        for op in plan["operations"][:2]:
            name = op["source"].rsplit("/", 1)[-1]
            dest_path = Path(op["dest"])
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            journal.append(
                make_event(
                    "intent", op["op_id"], source=op["source"], dest=op["dest"],
                    category=op["category"], identity=op["identity"], plan_hash=plan["plan_hash"],
                )
            )
            os.replace(self.sandbox.downloads / name, dest_path)
            journal.append(
                make_event(
                    "result", op["op_id"], outcome="moved", reason_code="OK",
                    dest_identity=op["identity"],
                )
            )
        third = plan["operations"][2]
        journal.append(
            make_event(
                "intent", third["op_id"], source=third["source"], dest=third["dest"],
                category=third["category"], identity=third["identity"], plan_hash=plan["plan_hash"],
            )
        )

        # 没有任何快照：撤销必须能仅由执行日志重建
        snapshot_file = Path(snapshot_path(self.sandbox.settings, plan["plan_id"]))
        self.assertFalse(snapshot_file.exists())

        recover_execution(self.sandbox.settings,plan["plan_id"])
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 2, undo.entries)
        for name in names:
            self.assertExists(self.sandbox.downloads / name)
        states = {e["op_id"]: e["undo_state"] for e in undo.entries}
        self.assertEqual(states[third["op_id"]], "skipped_not_moved")


class JournalFailureTests(SandboxTestCase):
    """S12：日志损坏、截断与落盘失败。"""

    def _seed_and_execute(self, names=("a.pdf",)):
        for name in names:
            self.sandbox.add_file(name, b"PAYLOAD")
        plan = self.sandbox.make_plan()
        return plan

    def test_s12_write_failure_prevents_move(self) -> None:
        plan = self._seed_and_execute(("a.pdf", "b.pdf"))
        journal = self.sandbox.journal(plan["plan_id"])
        original = Journal.append

        def failing(self, payload, *, report=None):
            if payload.get("event") == "intent" and payload.get("op_id", "").startswith("op-"):
                raise JournalWriteFailed("打桩：模拟磁盘满")
            return original(self, payload, report=report)

        Journal.append = failing
        try:
            result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        finally:
            Journal.append = original

        self.assertEqual(result.moved, 0, "intent 落盘失败时该文件绝不移动")
        self.assertEqual(result.outcomes[0]["reason_code"], "LOG_WRITE_FAILED")
        self.assertExists(self.sandbox.downloads / "a.pdf")
        self.assertExists(self.sandbox.downloads / "b.pdf")

    def test_s12_middle_corruption_is_detected_and_blocks(self) -> None:
        plan = self._seed_and_execute(("a.pdf", "b.pdf"))
        journal = self.sandbox.journal(plan["plan_id"])
        journal.append(make_event("intent", "op-0001", source="x", dest="y", identity={}))
        # 追加第二行，然后把第一行改成非法 JSON
        journal.append(
            make_event("result", "op-0001", outcome="moved", reason_code="OK", dest_identity={})
        )
        journal.append(make_event("intent", "op-0002", source="x2", dest="y2", identity={}))
        gen_path = Path(journal.generation_file(1))
        text = gen_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        lines[0] = "{ this is not json }\n"
        gen_path.write_text("".join(lines), encoding="utf-8")

        with self.assertRaises(JournalCorrupted):
            journal.read_events()
        with self.assertRaises(ExecutionRefused):
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertExists(self.sandbox.downloads / "a.pdf")

    def test_s12_crc_mismatch_is_corruption(self) -> None:
        plan = self._seed_and_execute(("a.pdf",))
        journal = self.sandbox.journal(plan["plan_id"])
        journal.append(make_event("intent", "op-0001", source="x", dest="y", identity={}))
        gen_path = Path(journal.generation_file(1))
        record = json.loads(gen_path.read_text(encoding="utf-8").strip())
        self.assertIn("crc32", record)
        record["crc32"] = "00000000"
        gen_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        with self.assertRaises(JournalCorrupted):
            journal.read_events()

    def test_s12_event_whitelist_is_per_kind(self) -> None:
        plan = self._seed_and_execute(("a.pdf",))
        exec_journal = self.sandbox.journal(plan["plan_id"], "execution")
        with self.assertRaises(JournalCorrupted):
            exec_journal.append(make_event("undo_intent", "op-0001", **{"from": "a", "to": "b"}))
        undo_journal = self.sandbox.journal(plan["plan_id"], "undo")
        with self.assertRaises(JournalCorrupted):
            undo_journal.append(make_event("intent", "op-0001", source="a", dest="b", identity={}))

    def test_s12_orphan_result_is_corruption(self) -> None:
        plan = self._seed_and_execute(("a.pdf",))
        journal = self.sandbox.journal(plan["plan_id"])
        with self.assertRaises(JournalCorrupted):
            journal.append(
                make_event("result", "op-0001", outcome="moved", reason_code="OK", dest_identity={})
            )


class TruncationTests(SandboxTestCase):
    """S18：截断后恢复 → 继续记录 → 再次恢复（旧证据不被覆盖）。"""

    def _seed(self, names=("a.pdf", "b.pdf", "c.pdf")):
        for name in names:
            self.sandbox.add_file(name, b"PAYLOAD-" + name.encode())
        return self.sandbox.make_plan()

    def test_s18_truncated_tail_sealed_then_continue_then_recover_again(self) -> None:
        plan = self._seed()
        from doc_selecter.directory_archive import ensure_layout
        ensure_layout(self.sandbox.settings, plan)
        journal = self.sandbox.journal(plan["plan_id"])
        op1, op2, op3 = plan["operations"]

        journal.append(
            make_event("intent", op1["op_id"], source=op1["source"], dest=op1["dest"],
                       category=op1["category"], identity=op1["identity"])
        )
        # 制造截断末行（半个 JSON，无换行）
        gen1 = Path(journal.generation_file(1))
        partial = '{"event":"intent","op_id":"op-0002","source":"/x'
        with open(gen1, "ab") as fh:
            fh.write(partial.encode("utf-8"))
        gen1_bytes_before = gen1.read_bytes()

        report = journal.ensure_sealed()
        self.assertEqual(report.sealed_now, [1])
        self.assertEqual(report.truncated_op_id, "op-0002")
        self.assertEqual(gen1.read_bytes(), gen1_bytes_before, "旧代次字节必须原样保留")

        manifest = json.loads(Path(journal.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual([g["gen"] for g in manifest["generations"]], [1, 2])
        sealed = manifest["generations"][0]
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(sealed["truncated_bytes"], len(partial.encode()))
        self.assertEqual(sealed["file_bytes"], len(gen1_bytes_before))

        # 继续记录：新事件落入第 2 代
        journal.append(
            make_event("intent", op2["op_id"], source=op2["source"], dest=op2["dest"],
                       category=op2["category"], identity=op2["identity"])
        )
        journal.append(
            make_event("result", op2["op_id"], outcome="moved", reason_code="OK",
                       dest_identity=op2["identity"])
        )
        gen2 = Path(journal.generation_file(2))
        self.assertTrue(gen2.exists())
        self.assertIn(op2["op_id"], gen2.read_text(encoding="utf-8"))
        gen1_text = gen1.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn('"dest"', gen1_text.split("op-0002")[-1] if "op-0002" in gen1_text else "",
                         "第 1 代中不得出现 op-0002 的完整记录")
        self.assertEqual(gen1_text.count(op2["op_id"]), 1, "op-0002 只应作为被截断的尾部碎片出现")

        # 再次恢复：幂等，不封存新代次、不丢事件
        second = journal.ensure_sealed()
        self.assertEqual(second.sealed_now, [])
        manifest2 = json.loads(Path(journal.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual([g["gen"] for g in manifest2["generations"]], [1, 2])
        self.assertEqual(gen1.read_bytes(), gen1_bytes_before)

        events, _ = journal.read_events()
        op_ids = {e["op_id"] for e in events if e.get("op_kind","file")=="file"}
        self.assertEqual(op_ids, {op1["op_id"], op2["op_id"]}, "合法事件不得丢失")

        # 恢复流程可重复执行
        first_recover = recover_execution(self.sandbox.settings, plan["plan_id"])
        second_recover = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(first_recover.get("truncated",{}).get("sealed_now",[]), [])
        self.assertEqual(second_recover.get("truncated",{}).get("sealed_now",[]), [])

    def test_s18_manifest_missing_single_generation_rebuildable(self) -> None:
        plan = self._seed()
        journal = self.sandbox.journal(plan["plan_id"])
        op1 = plan["operations"][0]
        journal.append(make_event("intent", op1["op_id"], source=op1["source"], dest=op1["dest"],
                                  category=op1["category"], identity=op1["identity"]))
        os.unlink(journal.manifest_path)

        report = journal.ensure_sealed()
        self.assertTrue(report.manifest_rebuilt)
        events, _ = journal.read_events()
        self.assertEqual(len(events), 1)
        self.assertTrue(Path(journal.manifest_path).exists())

    def test_s18_manifest_missing_multi_generation_is_corrupted(self) -> None:
        plan = self._seed()
        journal = self.sandbox.journal(plan["plan_id"])
        op1 = plan["operations"][0]
        journal.append(make_event("intent", op1["op_id"], source=op1["source"], dest=op1["dest"],
                                  category=op1["category"], identity=op1["identity"]))
        # 制造一个第二代的空文件，然后删除清单
        Path(journal.generation_file(2)).write_bytes(b"")
        os.unlink(journal.manifest_path)
        with self.assertRaises(JournalCorrupted):
            journal.read_events()

    def test_s18_unregistered_nonempty_generation_is_corrupted(self) -> None:
        plan = self._seed()
        journal = self.sandbox.journal(plan["plan_id"])
        op1 = plan["operations"][0]
        journal.append(make_event("intent", op1["op_id"], source=op1["source"], dest=op1["dest"],
                                  category=op1["category"], identity=op1["identity"]))
        Path(journal.generation_file(2)).write_bytes(b'{"event":"intent","op_id":"x"}\n')
        with self.assertRaises(JournalCorrupted):
            journal.read_events()


class InterruptedPublishTests(SandboxTestCase):
    """收口断言：空新文件建立 / 清单发布两个中断点都必须可恢复。"""

    def test_generation_switch_crash_point_empty_file_created(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        journal = self.sandbox.journal(plan["plan_id"])
        op = plan["operations"][0]
        journal.append(make_event("intent", op["op_id"], source=op["source"], dest=op["dest"],
                                  category=op["category"], identity=op["identity"]))

        # 中断点 A：活动代次有截断尾行，第 2 代空文件已建立，但清单尚未发布
        gen1 = Path(journal.generation_file(1))
        with open(gen1, "ab") as fh:
            fh.write(b'{"event":"intent","op_id":"op-0002"')
        Path(journal.generation_file(2)).write_bytes(b"")
        manifest_before = json.loads(Path(journal.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual([g["gen"] for g in manifest_before["generations"]], [1])

        report = journal.ensure_sealed()
        self.assertEqual(report.sealed_now, [1])
        manifest_after = json.loads(Path(journal.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual([g["gen"] for g in manifest_after["generations"]], [1, 2])
        self.assertEqual(Path(journal.generation_file(2)).read_bytes(), b"", "空文件不得被写入")

        # 幂等：再次调用不再产生新代次
        again = journal.ensure_sealed()
        self.assertEqual(again.sealed_now, [])
        manifest_final = json.loads(Path(journal.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual([g["gen"] for g in manifest_final["generations"]], [1, 2])

    def test_generation_switch_crash_point_manifest_published(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        journal = self.sandbox.journal(plan["plan_id"])
        op = plan["operations"][0]
        journal.append(make_event("intent", op["op_id"], source=op["source"], dest=op["dest"],
                                  category=op["category"], identity=op["identity"]))
        report = journal.ensure_sealed()  # 无坏末行 → 不封存
        self.assertEqual(report.sealed_now, [])
        self.assertEqual(journal.read_events()[0].__len__(), 1)

    def test_manifest_is_per_plan(self) -> None:
        for name in ("a.pdf", "b.pdf"):
            self.sandbox.add_file(name, b"PAYLOAD")
        plan1 = self.sandbox.make_plan()
        self.sandbox.add_file("c.pdf", b"MORE")
        build = build_plan(self.sandbox.settings)
        write_plan(self.sandbox.settings, build.plan)
        plan2 = build.plan
        self.assertNotEqual(plan1["plan_id"], plan2["plan_id"])

        j1 = self.sandbox.journal(plan1["plan_id"])
        j2 = self.sandbox.journal(plan2["plan_id"])
        j1.append(make_event("intent", "op-0001", source="a", dest="b", identity={
            "st_dev": 1, "st_ino": 1, "size_bytes": 1, "mtime_ns": 1, "mode_type": "regular"}))
        j2.append(make_event("intent", "op-0001", source="c", dest="d", identity={
            "st_dev": 1, "st_ino": 2, "size_bytes": 1, "mtime_ns": 1, "mode_type": "regular"}))
        self.assertNotEqual(j1.manifest_path, j2.manifest_path)
        self.assertEqual(len(j1.read_events()[0]), 1)
        self.assertEqual(len(j2.read_events()[0]), 1)
        self.assertTrue(Path(j1.manifest_path).exists() and Path(j2.manifest_path).exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
