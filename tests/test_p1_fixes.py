"""P1 修复回归测试（主控第三里程碑验收 REWORK 的六组反例）。

对应关系：

- A 能力探测：``ProbeIsolationTests``
- B 路径越界 / 链接绕过：``PathContainmentTests``
- C 撤销误报 restored：``RestoreIdentityTests``
- D 损坏日志被当空历史：``CorruptionHandlingTests``
- E 既有全局阻塞未阻止撤销：``PriorGlobalBlockTests``

全部反例都转为**正向安全断言**：修复后的行为必须是「拒绝 / 停止 / 保留证据」，
而不是「移动成功」或「报告 restored」。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from tests.harness import Sandbox, SandboxTestCase, snapshot_dir

from doc_selecter import undo as undo_mod
from doc_selecter.atomic_move import AtomicMover, lstat_identity
from doc_selecter.config import ConfigError, load_settings
from doc_selecter.executor import ExecutionRefused, execute_plan, recover_execution
from doc_selecter.journal import Journal, JournalCorrupted, make_event
from doc_selecter.planner import PlanError, build_plan, load_verified_plan, write_confirmation, write_plan
from doc_selecter.snapshot import read_snapshot, snapshot_path
from doc_selecter.undo import UndoRefused, load_or_rebuild_snapshot, undo_plan

SENTINELS = (".probe-src-a", ".probe-dst-a", ".probe-src-b", ".probe-dst-b")


class ProbeIsolationTests(SandboxTestCase):
    """A：能力探测不得删除已有 ``.probe-*`` 文件（含并发）。"""

    def _make_sentinels(self) -> list[Path]:
        files = []
        for name in SENTINELS:
            path = self.sandbox.state / name
            path.write_bytes(b"preexisting user sentinel")
            os.utime(path, ns=(123456789, 123456789))
            files.append(path)
        return files

    def test_probe_preserves_preexisting_sentinels(self) -> None:
        files = self._make_sentinels()
        before = {p.name: (p.read_bytes(), os.lstat(p).st_ino, os.lstat(p).st_mtime_ns) for p in files}

        AtomicMover().probe_capability(str(self.sandbox.state))

        after = {p.name: (p.read_bytes(), os.lstat(p).st_ino, os.lstat(p).st_mtime_ns) for p in files}
        self.assertEqual(before, after, "既有 .probe-* 哨兵必须逐字节、inode、mtime 保持不变")
        for path in files:
            self.assertTrue(path.exists(), f"哨兵不得被删除：{path}")

    def test_probe_leaves_no_residue(self) -> None:
        self._make_sentinels()
        before_names = {p.name for p in self.sandbox.state.iterdir()}
        AtomicMover().probe_capability(str(self.sandbox.state))
        after_names = {p.name for p in self.sandbox.state.iterdir()}
        self.assertEqual(before_names, after_names, "探测不得留下任何残留条目")

    def test_concurrent_probes_do_not_share_materials(self) -> None:
        """多个并发探测使用各自的唯一目录，互不删除/干扰。"""
        files = self._make_sentinels()
        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def run() -> None:
            try:
                barrier.wait(timeout=10)
                AtomicMover().probe_capability(str(self.sandbox.state))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [], f"并发探测不得失败：{errors}")
        for path in files:
            self.assertTrue(path.exists(), f"并发探测不得删除哨兵：{path}")
            self.assertEqual(path.read_bytes(), b"preexisting user sentinel")
        residue = [p.name for p in self.sandbox.state.iterdir() if p.name.startswith(".probe-")]
        self.assertEqual(sorted(residue), sorted(SENTINELS), "不得留下本次探测的残留材料")

    def test_execute_and_undo_succeed_despite_probe_sentinels(self) -> None:
        """状态目录里存在同名历史探测文件时，正常闭环仍必须工作。"""
        self._make_sentinels()
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 1, result.outcomes)
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 1, undo.entries)
        for name in SENTINELS:
            self.assertTrue((self.sandbox.state / name).exists(), "哨兵必须始终保留")

    def test_probe_runs_inside_global_lock(self) -> None:
        """探测必须在全局锁内：持锁进程存在时，执行在探测前就被拒绝。"""
        from doc_selecter.locking import GlobalLock, LockBusy

        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        lock = GlobalLock(self.sandbox.settings)
        handle = lock.acquire(kind="cli", action="execute", plan_id="holder")
        try:
            with self.assertRaises((ExecutionRefused, LockBusy)):
                execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        finally:
            lock.release(handle)
        self.assertExists(self.sandbox.downloads / "a.pdf")


class PathContainmentTests(SandboxTestCase):
    """B：拒绝越界路径与链接绕过（必须在建目标目录/移动之前）。"""

    def _settings_with(self, target_parent_name: str):
        cfg = self.sandbox.root / "config.json"
        cfg.write_text(json.dumps({
            "downloads_dir": str(self.sandbox.downloads),
            "state_dir": str(self.sandbox.state),
            "target_parent_name": target_parent_name,
        }), encoding="utf-8")
        return load_settings(str(cfg), env={})

    def test_traversal_parent_name_rejected_at_config(self) -> None:
        for bad in ("../Else", "..", ".", "a/b", "/abs", "Else/..", "甲/乙"):
            with self.subTest(name=bad):
                with self.assertRaises(ConfigError):
                    self._settings_with(bad)

    def test_absolute_parent_name_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._settings_with("/tmp/abs-else")

    def test_legal_parent_name_accepted(self) -> None:
        settings = self._settings_with("下载分类")
        self.assertEqual(settings.target_parent_name, "下载分类")
        self.assertTrue(settings.target_root.endswith("下载分类"))

    def test_traversal_never_moves_outside(self) -> None:
        """复现主控反例一：越界名必须在配置阶段就被拒绝，绝不移动。"""
        self.sandbox.add_file("report.pdf", b"owned synthetic document")
        outside = self.sandbox.root / "Else"
        with self.assertRaises(ConfigError):
            self._settings_with("../Else")
        self.assertNotExists(outside)
        self.assertExists(self.sandbox.downloads / "report.pdf")

    def test_symlink_root_rejected_at_config(self) -> None:
        """复现主控反例二：``下载分类`` 是指向外部的链接 → 配置阶段拒绝。"""
        outside = self.sandbox.root / "Else"
        outside.mkdir()
        (self.sandbox.downloads / "下载分类").symlink_to(outside, target_is_directory=True)
        self.sandbox.add_file("report.pdf", b"owned synthetic document")
        with self.assertRaises(ConfigError) as ctx:
            self.sandbox.settings
        self.assertIn("符号链接", str(ctx.exception))
        self.assertNotExists(outside / "文档资料" / "report.pdf")
        self.assertExists(self.sandbox.downloads / "report.pdf")
        self.assertEqual(snapshot_dir(outside), {}, "外部目录必须保持为空")

    def test_symlinked_category_dir_rejected_before_move(self) -> None:
        """类别目录是链接 → 计划绑定期拒绝，且不创建目标目录、不移动。"""
        self.sandbox.add_file("report.pdf", b"owned synthetic document")
        outside = self.sandbox.root / "ElseCat"
        outside.mkdir()
        target_root = self.sandbox.downloads / "下载分类"
        target_root.mkdir()
        plan = self.sandbox.make_plan(confirm=False)
        (target_root / "文档资料").symlink_to(outside, target_is_directory=True)

        # plan = self.sandbox.make_plan(confirm=False)  # 只读计划可保存，当前确认必须拒绝
        with self.assertRaises(PlanError):
            write_confirmation(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        with self.assertRaises(PlanError) as ctx:
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertIn("符号链接", str(ctx.exception))
        self.assertExists(self.sandbox.downloads / "report.pdf")
        self.assertEqual(snapshot_dir(outside), {}, "外部目录必须保持为空")

    def test_plan_binding_rejects_mismatched_downloads_dir(self) -> None:
        """载入计划时绑定当前配置：换一个下载目录后，同一计划必须被拒绝。"""
        self.sandbox.add_file("report.pdf", b"X")
        plan = self.sandbox.make_plan()
        other = self.sandbox.root / "OtherDownloads"
        other.mkdir()
        cfg = self.sandbox.root / "other.json"
        cfg.write_text(json.dumps({
            "downloads_dir": str(other),
            "state_dir": str(self.sandbox.state),
            "target_parent_name": "下载分类",
        }), encoding="utf-8")
        other_settings = load_settings(str(cfg), env={})

        with self.assertRaises(PlanError) as ctx:
            load_verified_plan(other_settings, plan["plan_id"], verify_binding=True)
        self.assertIn("不一致", str(ctx.exception))

    def test_plan_binding_rejects_dest_that_escapes_category(self) -> None:
        """计划被篡改成「目标逃出类别目录」时，绑定校验必须拒绝。"""
        self.sandbox.add_file("report.pdf", b"X")
        plan = self.sandbox.make_plan(confirm=False)
        # 直接改内存中的操作（不改文件），验证绑定校验本身
        from doc_selecter.planner import verify_plan_binding

        broken = json.loads(json.dumps(plan))
        broken["operations"][0]["dest"] = str(self.sandbox.root / "Else" / "report.pdf")
        with self.assertRaises(PlanError):
            verify_plan_binding(self.sandbox.settings, broken)

    def test_plan_binding_rejects_renamed_dest(self) -> None:
        self.sandbox.add_file("report.pdf", b"X")
        plan = self.sandbox.make_plan(confirm=False)
        from doc_selecter.planner import verify_plan_binding

        broken = json.loads(json.dumps(plan))
        broken["operations"][0]["dest"] = broken["operations"][0]["dest"] + ".renamed"
        with self.assertRaises(PlanError):
            verify_plan_binding(self.sandbox.settings, broken)

    def test_plan_binding_rejects_nested_source(self) -> None:
        self.sandbox.add_file("report.pdf", b"X")
        plan = self.sandbox.make_plan(confirm=False)
        from doc_selecter.planner import verify_plan_binding

        broken = json.loads(json.dumps(plan))
        broken["operations"][0]["source"] = str(self.sandbox.downloads / "sub" / "report.pdf")
        with self.assertRaises(PlanError):
            verify_plan_binding(self.sandbox.settings, broken)

    def test_normal_roundtrip_still_works(self) -> None:
        """不得以「全面拒绝正常移动」来修复越界问题。"""
        self.sandbox.add_file("report.pdf", b"PAYLOAD")
        self.sandbox.add_file("archive.zip", b"ZIP")
        plan = self.sandbox.make_plan()
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 2, result.outcomes)
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 2, undo.entries)


class RestoreIdentityTests(SandboxTestCase):
    """C：写 restored 之前必须核对原位置五项身份。"""

    def _executed(self, name: str = "report.pdf") -> tuple[dict, Path]:
        original = self.sandbox.add_file(name, b"original synthetic document")
        plan = self.sandbox.make_plan()
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 1, result.outcomes)
        return plan, original

    def test_restore_reports_failure_when_target_replaced_after_check(self) -> None:
        """复现主控反例三：检查后目标被替换 → 绝不报 restored。"""
        plan, original = self._executed()
        op = plan["operations"][0]
        expected = op["identity"]
        preserved = self.sandbox.root / "preserved-original.pdf"

        def hook(stage: str, info: dict) -> None:
            if stage == "before_undo_move":
                dest = Path(info["from"])
                dest.rename(preserved)                       # 挪走真身
                dest.write_bytes(b"different synthetic replacement")

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True, hook=hook)

        self.assertEqual(undo.restored, 0, "绝不能在身份不匹配时报告 restored")
        self.assertTrue(undo.stopped, "身份异常必须全局停止")
        actual = lstat_identity(str(original))
        self.assertIsNotNone(actual)
        self.assertFalse(actual.matches(expected), "原位置放的是替换物，不是原文件")
        self.assertTrue(preserved.exists(), "原文件必须仍被保存在沙盒内，不得丢失")

        snapshot = read_snapshot(self.sandbox.settings, plan["plan_id"])
        self.assertNotEqual(snapshot["entries"][0]["undo_state"], "restored")
        self.assertIn(snapshot["entries"][0]["undo_state"], {"unknown_needs_review"})

    def test_restore_identity_mismatch_is_recorded_with_evidence(self) -> None:
        plan, _ = self._executed()
        preserved = self.sandbox.root / "kept.pdf"

        def hook(stage: str, info: dict) -> None:
            if stage == "before_undo_move":
                dest = Path(info["from"])
                dest.rename(preserved)
                dest.write_bytes(b"replacement")

        undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True, hook=hook)
        journal = self.sandbox.journal(plan["plan_id"], "undo")
        events, _ = journal.read_events()
        mismatch = [
            e for e in events
            if e.get("reason_code") == "RESTORED_IDENTITY_MISMATCH"
        ]
        self.assertTrue(mismatch, "必须持久化身份不匹配的证据")
        self.assertIn("observed_identity", mismatch[-1])
        self.assertIn("expected_identity", mismatch[-1])

    def test_identity_mismatch_global_state_blocks_further_undo(self) -> None:
        """全局停止后，再次撤销不得继续搬动其他文件。"""
        for name in ("a.pdf", "b.pdf"):
            self.sandbox.add_file(name, b"PAYLOAD-" + name.encode())
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        a_op = next(op for op in plan["operations"] if op["source"].endswith("a.pdf"))
        preserved = self.sandbox.root / "kept-a.pdf"

        def hook(stage: str, info: dict) -> None:
            if stage == "before_undo_move" and str(info["from"]).endswith("a.pdf"):
                dest = Path(info["from"])
                dest.rename(preserved)
                dest.write_bytes(b"replacement")

        first = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True, hook=hook)
        self.assertTrue(first.stopped, "身份异常必须全局停止")
        self.assertEqual(first.restored, 1, "逆序撤销：另一项在全局停止前已合法还原")
        a_state = next(e["undo_state"] for e in first.entries if e["op_id"] == a_op["op_id"])
        self.assertEqual(a_state, "unknown_needs_review")
        b_dest = next(op["dest"] for op in plan["operations"] if op["source"].endswith("b.pdf"))
        b_src = self.sandbox.downloads / "b.pdf"

        # 第二次撤销：既有全局状态必须继续阻止其他移动
        second = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(second.restored, 0, "既有全局阻塞未解决前不得再撤销其他文件")
        self.assertTrue(second.stopped)
        self.assertEqual(second.undo_status, "needs_review")
        self.assertFalse(Path(b_dest).exists(), "已经还原的项不得被再次搬动")
        self.assertTrue(b_src.exists(), "已经还原的文件必须留在原位置")
        self.assertEqual(b_src.read_bytes(), b"PAYLOAD-b.pdf")

    def test_normal_restore_still_records_restored_with_identity(self) -> None:
        """不得以「全面拒绝撤销」来修复误报：正常撤销仍必须成功。"""
        plan, original = self._executed()
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 1, undo.entries)
        self.assertIdentityMatches(original, plan["operations"][0]["identity"])
        journal = self.sandbox.journal(plan["plan_id"], "undo")
        events, _ = journal.read_events()
        restored = [e for e in events if e.get("undo_state") == "restored"]
        self.assertTrue(restored)
        self.assertIn("expected_identity", restored[-1])
        self.assertIn("observed_identity", restored[-1])


class CorruptionHandlingTests(SandboxTestCase):
    """D：损坏日志不得被当作空撤销历史。"""

    def _executed_with_global_event(self) -> tuple[dict, Path]:
        self.sandbox.add_file("report.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 1, result.outcomes)
        op = plan["operations"][0]
        journal = self.sandbox.journal(plan["plan_id"], "undo")
        journal.append(make_event(
            "undo_skip", op["op_id"], undo_state="unknown_needs_review",
            reason_code="UNKNOWN_STATE", reason="synthetic global issue",
            expected_identity=op["identity"],
        ))
        gen = Path(journal.generation_file(1))
        gen.write_bytes(b"not-json\n" + gen.read_bytes())
        return plan, gen

    def test_recover_marks_corrupted_and_keeps_evidence(self) -> None:
        plan, gen = self._executed_with_global_event()
        original_bytes = gen.read_bytes()

        with self.assertRaises(JournalCorrupted):
            self.sandbox.journal(plan["plan_id"], "undo").fold()

        snapshot_file = Path(snapshot_path(self.sandbox.settings, plan["plan_id"]))
        snapshot_bytes_before = snapshot_file.read_bytes()
        state_file = self.sandbox.state / "plan_state" / f"{plan['plan_id']}.json"
        phase_before = json.loads(state_file.read_text(encoding="utf-8"))["phase"]
        self.assertEqual(phase_before, "executed")

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertEqual(report["phase"], "corrupted", "损坏必须进入 corrupted")
        self.assertEqual(gen.read_bytes(), original_bytes, "坏日志字节必须原样保留")
        from doc_selecter.locking import current_phase

        self.assertEqual(current_phase(self.sandbox.settings, plan["plan_id"]), "corrupted",
                         "收尾不得把 corrupted 覆盖为 executed")
        # 既有快照必须逐字节保留：损坏时不得重建/覆盖
        self.assertEqual(snapshot_file.read_bytes(), snapshot_bytes_before,
                         "损坏时既有快照必须保持逐字节不变")
        snapshot = read_snapshot(self.sandbox.settings, plan["plan_id"])
        self.assertIsNotNone(snapshot)

    def test_rebuild_raises_instead_of_returning_empty_history(self) -> None:
        plan, gen = self._executed_with_global_event()
        original_bytes = gen.read_bytes()
        Path(snapshot_path(self.sandbox.settings, plan["plan_id"])).unlink()

        with self.assertRaises(JournalCorrupted):
            load_or_rebuild_snapshot(self.sandbox.settings, plan["plan_id"])

        self.assertEqual(gen.read_bytes(), original_bytes)
        self.assertFalse(Path(snapshot_path(self.sandbox.settings, plan["plan_id"])).exists(),
                         "损坏时不得重建快照")

    def test_execute_and_undo_refused_on_corrupted_logs(self) -> None:
        plan, gen = self._executed_with_global_event()
        original_bytes = gen.read_bytes()

        with self.assertRaises((ExecutionRefused, UndoRefused, JournalCorrupted)):
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        with self.assertRaises((UndoRefused, JournalCorrupted)):
            undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)

        self.assertEqual(gen.read_bytes(), original_bytes, "拒绝过程中不得改动坏日志")
        # 该计划此前已成功执行（文件在目标位置）；损坏只会让后续操作被拒绝，不会搬动任何东西
        self.assertTrue(Path(plan["operations"][0]["dest"]).exists(),
                        "损坏时不得把已移动的文件搬回或再移动")
        self.assertNotExists(self.sandbox.downloads / "report.pdf")

    def test_corrupted_execution_log_also_blocks(self) -> None:
        self.sandbox.add_file("report.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        journal = self.sandbox.journal(plan["plan_id"], "execution")
        gen = Path(journal.generation_file(1))
        gen.write_bytes(b"{ broken\n" + gen.read_bytes())
        original_bytes = gen.read_bytes()

        report = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(report["phase"], "corrupted")
        self.assertEqual(gen.read_bytes(), original_bytes)

    def test_healthy_logs_still_rebuild_snapshot(self) -> None:
        """不得以「一律拒绝重建」修复：健康日志必须仍能重建快照。"""
        self.sandbox.add_file("report.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        Path(snapshot_path(self.sandbox.settings, plan["plan_id"])).unlink()

        snapshot, _ = load_or_rebuild_snapshot(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(snapshot["entries"][0]["undo_state"], "pending")
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 1, undo.entries)


class PriorGlobalBlockTests(SandboxTestCase):
    """E：已持久化的全局未知/目录异常必须阻止其他项继续撤销。"""

    def _executed_two(self) -> dict:
        for name in ("a.pdf", "b.pdf"):
            self.sandbox.add_file(name, b"PAYLOAD-" + name.encode())
        plan = self.sandbox.make_plan()
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 2, result.outcomes)
        return plan

    def _write_global_skip(self, plan: dict, state: str) -> str:
        op = plan["operations"][0]
        self.sandbox.journal(plan["plan_id"], "undo").append(make_event(
            "undo_skip", op["op_id"], undo_state=state, reason_code="UNKNOWN_STATE",
            reason="synthetic persistent global issue", expected_identity=op["identity"],
        ))
        return op["op_id"]

    def test_prior_unknown_stops_batch(self) -> None:
        """复现主控反例五。"""
        plan = self._executed_two()
        first_op = self._write_global_skip(plan, "unknown_needs_review")

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)

        self.assertEqual(undo.restored, 0, "既有全局未知状态存在时不得撤销其他文件")
        self.assertTrue(undo.stopped)
        self.assertEqual(undo.undo_status, "needs_review")
        states = {e["op_id"]: e["undo_state"] for e in undo.entries}
        self.assertEqual(states[first_op], "unknown_needs_review")
        second = [op for op in plan["operations"] if op["op_id"] != first_op][0]
        self.assertEqual(states[second["op_id"]], "pending", "其他项应保持 pending，不得被改动")
        self.assertTrue(Path(second["dest"]).exists(), "目标文件不得被搬动")
        self.assertNotExists(self.sandbox.downloads / os.path.basename(second["source"]))

    def test_prior_blocked_dir_changed_stops_batch(self) -> None:
        plan = self._executed_two()
        self._write_global_skip(plan, "blocked_dir_changed")

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 0)
        self.assertTrue(undo.stopped)

    def test_prior_local_conflict_still_continues(self) -> None:
        """local 冲突（原位置被占用）必须仍然跳过该项并继续其他项。"""
        plan = self._executed_two()
        first = plan["operations"][0]
        intruder = self.sandbox.downloads / os.path.basename(first["source"])
        intruder.write_bytes(b"USER FILE")
        before = intruder.read_bytes()

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)

        states = {e["op_id"]: e["undo_state"] for e in undo.entries}
        self.assertEqual(states[first["op_id"]], "blocked_source_occupied")
        self.assertEqual(undo.restored, 1, "local 冲突不得阻止其他项")
        self.assertFalse(undo.stopped)
        self.assertEqual(undo.undo_status, "partial_undo")
        self.assertEqual(intruder.read_bytes(), before)

    def test_normal_pending_still_restores(self) -> None:
        """不得把全部 pending 改成 unknown：无全局状态的批次必须正常还原。"""
        plan = self._executed_two()
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 2, undo.entries)
        self.assertFalse(undo.stopped)
        self.assertEqual(undo.undo_status, "completed")

    def test_attempting_resume_still_works_after_fix(self) -> None:
        """合法续作不得被新的整批准入误伤。"""
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        op = plan["operations"][0]
        journal = self.sandbox.journal(plan["plan_id"], "undo")
        journal.append(make_event(
            "undo_intent", op["op_id"],
            **{"from": op["dest"], "to": op["source"], "expected_identity": op["identity"]},
        ))

        first = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True, resume=False)
        self.assertEqual(first.restored, 0, "未获续作确认时不动文件")
        events = [e["event"] for e in journal.read_events()[0]]
        self.assertEqual(events.count("undo_intent"), 1, "不得重复追加 undo_intent")

        final = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True, resume=True)
        self.assertEqual(final.restored, 1, final.entries)
        events = [e["event"] for e in journal.read_events()[0]]
        self.assertEqual(events.count("undo_intent"), 1)
        self.assertEqual(events.count("undo_result"), 1)

    def test_prior_global_recheck_after_resolution_allows_restore(self) -> None:
        """全局阻塞被明确解决（改为 restored）后，允许继续撤销其他项。"""
        plan = self._executed_two()
        first_op = self._write_global_skip(plan, "unknown_needs_review")
        blocked = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(blocked.restored, 0)

        # 人工「解决」：把该项标记为 restored（模拟用户确认已完成）
        from doc_selecter.snapshot import UNDO_RESTORED, write_snapshot

        snapshot = read_snapshot(self.sandbox.settings, plan["plan_id"])
        for entry in snapshot["entries"]:
            if entry["op_id"] == first_op:
                entry["undo_state"] = UNDO_RESTORED
        write_snapshot(self.sandbox.settings, plan["plan_id"], snapshot)

        # 仍需通过撤销日志物化：先写 undo_intent，再写配对的 undo_result
        journal = self.sandbox.journal(plan["plan_id"], "undo")
        journal.append(make_event(
            "undo_intent", first_op, **{"from": plan["operations"][0]["dest"], "to": plan["operations"][0]["source"],
                                        "expected_identity": plan["operations"][0]["identity"]},
        ))
        journal.append(make_event(
            "undo_result", first_op, outcome="restored", reason_code="OK",
            undo_state=UNDO_RESTORED, reason="人工确认已完成",
        ))
        resumed = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(resumed.restored, 1, "解决全局阻塞后应能继续还原剩余项")
        self.assertFalse(resumed.stopped)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
