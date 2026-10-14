"""主控第三里程碑**复验**（REWORK）七组反例的回归断言。

对应 `docs` 之外的主控证据：
`/private/tmp/docsel-reaccept-ot1hldu3/{第三里程碑复验.md, additional_checks.py,
additional-results.json}`。

四组问题：

1. ``InternalLinkTests``：原始相对段的链接检查（内部类别链接不得被 realpath 抹掉）
2. ``EntryBindingTests``：undo/recover 必须绑定当前配置；目录身份实际记录并校验
3. ``ValidationFirstTests``：两份日志必须在任何移动/补写/封存/快照替换之前只读校验
4. ``UndoLockOrderTests``：撤销必须在锁内校验/准入后才探测
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.harness import Sandbox, SandboxTestCase

from doc_selecter import undo as undo_module
from doc_selecter.atomic_move import AtomicMover
from doc_selecter.config import ConfigError, load_settings
from doc_selecter.executor import ExecutionRefused, execute_plan, recover_execution
from doc_selecter.journal import JournalCorrupted, make_event
from doc_selecter.locking import GlobalLock, LockBusy, current_phase
from doc_selecter.plan_hash import PlanHashError, reject_symlink_segments_between
from doc_selecter.planner import (
    PlanError,
    build_plan,
    capture_dir_identities,
    load_verified_plan,
    verify_dir_identity,
    verify_plan_binding,
    write_confirmation,
    write_plan,
)
from doc_selecter.snapshot import read_snapshot, snapshot_path
from doc_selecter.undo import UndoRefused, undo_plan

from doc_selecter.config import TEST_ROOT_ENV


class _Base(SandboxTestCase):
    def _prepare(self, name: str = "report.pdf", *, confirm: bool = True) -> dict:
        self.sandbox.add_file(name, b"owned synthetic payload")
        return self.sandbox.make_plan(confirm=confirm)

    def _execute(self, plan: dict):
        return execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])

    def _alternate_settings(self):
        """另一份**合法**配置：downloads_dir 指向别处，state_dir 不变。"""
        other = self.sandbox.root / "OtherDownloads"
        other.mkdir(exist_ok=True)
        cfg = self.sandbox.root / "other-config.json"
        cfg.write_text(json.dumps({
            "downloads_dir": str(other),
            "state_dir": str(self.sandbox.state),
        }), encoding="utf-8")
        return load_settings(str(cfg), env={})

    def _corrupt_undo_log(self, plan: dict) -> Path:
        op = plan["operations"][0]
        journal = self.sandbox.journal(plan["plan_id"], "undo")
        journal.append(make_event(
            "undo_skip", op["op_id"], undo_state="unknown_needs_review",
            reason_code="UNKNOWN_STATE", reason="synthetic event",
            expected_identity=op["identity"],
        ))
        gen = Path(journal.generation_file(1))
        gen.write_bytes(b"not-json\n" + gen.read_bytes())
        return gen


class InternalLinkTests(_Base):
    """1：内部类别链接必须在绑定阶段被拒绝（不能被 realpath 抹掉）。"""

    def _link_category(self) -> Path:
        root = self.sandbox.downloads / "下载分类"
        root.mkdir(exist_ok=True)
        actual = root / "音视频"
        actual.mkdir(exist_ok=True)
        link = root / "文档资料"
        if not link.exists() and not link.is_symlink():
            link.symlink_to(actual, target_is_directory=True)
        return link

    def test_helper_rejects_internal_link(self) -> None:
        link = self._link_category()
        with self.assertRaises(PlanHashError) as ctx:
            reject_symlink_segments_between(str(self.sandbox.downloads), str(link))
        self.assertIn("符号链接", str(ctx.exception))

    def test_internal_link_moves_nothing(self) -> None:
        """复现主控反例 `internal_link`：必须零移动且目标目录不被写入。"""
        plan = self._prepare(confirm=False)
        link = self._link_category()
        with self.assertRaises(PlanError):
            write_confirmation(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        actual_dir = self.sandbox.downloads / "下载分类" / "音视频"
        before = sorted(p.name for p in actual_dir.iterdir())

        with self.assertRaises(PlanError) as ctx:
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertIn("符号链接", str(ctx.exception))
        self.assertExists(self.sandbox.downloads / "report.pdf")
        self.assertEqual(sorted(p.name for p in actual_dir.iterdir()), before,
                         "链接目标的真实目录不得被写入")
        self.assertTrue(link.is_symlink())

    def test_internal_link_rejected_by_binding_directly(self) -> None:
        plan = self._prepare(confirm=False)
        self._link_category()
        with self.assertRaises(PlanError):
            write_confirmation(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        with self.assertRaises(PlanError):
            verify_plan_binding(self.sandbox.settings, plan)

    def test_category_link_replaced_after_plan_is_rejected(self) -> None:
        """计划生成后才把类别目录换成链接 → 执行时必须拒绝。"""
        plan = self._prepare()
        self._link_category()
        with self.assertRaises(PlanError):
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertExists(self.sandbox.downloads / "report.pdf")

    def test_real_file_parent_chain_and_system_alias_not_affected(self) -> None:
        """正常文件父链与 macOS /var 系统别名不得被误伤。"""
        self.sandbox.add_file("a.pdf", b"X")
        target_root = self.sandbox.downloads / "下载分类"
        category = target_root / "文档资料"
        category.mkdir(parents=True)
        target = category / "a.pdf"
        target.write_bytes(b"X")
        reject_symlink_segments_between(str(self.sandbox.downloads), str(target_root))
        reject_symlink_segments_between(str(self.sandbox.downloads), str(target), skip_last=True)

    def test_normal_execution_still_works_with_own_target_root(self) -> None:
        plan = self._prepare()
        result = self._execute(plan)
        self.assertEqual(result.moved, 1, result.outcomes)


class EntryBindingTests(_Base):
    """2：undo/recover 必须绑定当前配置；目录身份真实记录并校验。"""

    def test_undo_rejected_after_downloads_dir_changed(self) -> None:
        """复现主控反例 `undo_binding`：换配置后旧计划必须被拒绝。"""
        plan = self._prepare()
        self.assertEqual(self._execute(plan).moved, 1)
        other = self._alternate_settings()

        with self.assertRaises(UndoRefused) as ctx:
            undo_plan(other, plan["plan_id"], confirm=True)
        self.assertIn("绑定", str(ctx.exception))
        # A 目录里的文件保持已移动状态，未被越权还原
        self.assertNotExists(self.sandbox.downloads / "report.pdf")
        self.assertTrue(Path(plan["operations"][0]["dest"]).exists())

    def test_recover_rejected_after_downloads_dir_changed(self) -> None:
        """复现主控反例 `recover_binding`。"""
        plan = self._prepare()
        self.assertEqual(self._execute(plan).moved, 1)
        other = self._alternate_settings()

        with self.assertRaises(PlanError) as ctx:
            recover_execution(other, plan["plan_id"])
        self.assertIn("不一致", str(ctx.exception))

    def test_undo_and_recover_still_work_with_matching_config(self) -> None:
        plan = self._prepare()
        self.assertEqual(self._execute(plan).moved, 1)
        report = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertIn(report["phase"], {"executed", "undone", "partial_undo", "needs_review"})
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 1, undo.entries)

    def test_plan_records_dir_identities(self) -> None:
        plan = self._prepare()
        identities = plan.get("dir_identities")
        self.assertIsInstance(identities, dict)
        self.assertIn("downloads_dir", identities)
        dev, ino = identities["downloads_dir"]
        self.assertEqual((dev, ino), (os.lstat(self.sandbox.downloads).st_dev,
                                      os.lstat(self.sandbox.downloads).st_ino))
        # 目标根在预览时尚未创建 → 记为 null，且不参与哈希
        self.assertIsNone(identities["target_root"])
        from doc_selecter.plan_hash import compute_plan_hash

        copy = json.loads(json.dumps(plan))
        copy["dir_identities"] = {"downloads_dir": [0, 0], "target_root": [1, 1]}
        self.assertNotEqual(compute_plan_hash(copy), plan["plan_hash"],
                            "v3 原父链身份须参与 plan_hash")

    def test_replaced_downloads_dir_is_rejected(self) -> None:
        """下载目录被替换（inode 变化）时，绑定校验必须拒绝。"""
        plan = self._prepare()
        settings = self.sandbox.settings
        identities = capture_dir_identities(settings)
        # 用一个全新目录替换下载目录（同一路径、不同 inode）
        moved = self.sandbox.root / "old-downloads"
        self.sandbox.downloads.rename(moved)
        self.sandbox.downloads.mkdir()
        with self.assertRaises(PlanError) as ctx:
            verify_dir_identity("下载目录", str(self.sandbox.downloads),
                                identities["downloads_dir"])
        self.assertIn("身份", str(ctx.exception))

    def test_target_root_identity_recorded_and_replacement_rejected(self) -> None:
        """目标根在预览时已存在 → 记录身份；被替换后必须拒绝。"""
        self.sandbox.add_file("report.pdf", b"X")
        target = self.sandbox.downloads / "下载分类"
        target.mkdir(parents=True, exist_ok=True)
        plan = self.sandbox.make_plan()
        settings = self.sandbox.settings

        verified = load_verified_plan(settings, plan["plan_id"], verify_binding=True)
        recorded = verified["dir_identities"]["target_root"]
        self.assertIsNotNone(recorded, "目标根已存在时必须记录身份")
        self.assertEqual(tuple(recorded),
                         (os.lstat(target).st_dev, os.lstat(target).st_ino))

        # 替换目标根（同一路径、不同 inode）→ 绑定校验必须拒绝
        target.rename(self.sandbox.root / "old-target")
        target.mkdir()
        with self.assertRaises(PlanError) as ctx:
            load_verified_plan(settings, plan["plan_id"], verify_binding=True)
        self.assertIn("身份", str(ctx.exception))

    def test_binding_works_when_target_root_absent(self) -> None:
        """预览时目标目录尚未创建：绑定校验必须放行（不算失败）。"""
        plan = self._prepare()
        self.assertFalse(Path(plan["target_root"]).exists())
        load_verified_plan(self.sandbox.settings, plan["plan_id"], verify_binding=True)


class ValidationFirstTests(_Base):
    """3：两份日志必须在任何写入/移动之前只读校验。"""

    def test_execute_refused_before_any_move_when_undo_log_corrupt(self) -> None:
        """复现主控反例 `execute_validation_order`：零移动。"""
        plan = self._prepare()
        gen = self._corrupt_undo_log(plan)
        before = gen.read_bytes()

        with self.assertRaises(ExecutionRefused) as ctx:
            self._execute(plan)
        self.assertIn("撤销日志损坏", str(ctx.exception))
        self.assertEqual(gen.read_bytes(), before, "拒绝过程中不得改动坏日志")
        self.assertExists(self.sandbox.downloads / "report.pdf")
        self.assertNotExists(Path(plan["operations"][0]["dest"]))
        # 执行日志也不得被写入任何事件
        exec_journal = self.sandbox.journal(plan["plan_id"], "execution")
        self.assertEqual(exec_journal.read_events(allow_repair=True)[0], [])

    def test_recover_writes_nothing_when_undo_log_corrupt(self) -> None:
        """复现主控反例 `recover_validation_order`：不补写执行 result。"""
        plan = self._prepare()
        op = plan["operations"][0]
        journal = self.sandbox.journal(plan["plan_id"], "execution")
        journal.append(make_event(
            "intent", op["op_id"], source=op["source"], dest=op["dest"],
            category=op["category"], identity=op["identity"],
        ))
        Path(op["dest"]).parent.mkdir(parents=True, exist_ok=True)
        AtomicMover().rename_excl(op["source"], op["dest"])

        bad = self._corrupt_undo_log(plan)
        bad_before = bad.read_bytes()
        gen = Path(journal.generation_file(1))
        exec_before = gen.read_bytes()

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertEqual(report["phase"], "corrupted")
        self.assertEqual(gen.read_bytes(), exec_before,
                         "损坏时不得补写任何正常事件（执行日志必须逐字节不变）")
        self.assertEqual(bad.read_bytes(), bad_before, "坏日志必须原样保留")
        self.assertEqual(report["recovered"], [], "不得报告任何补记")

    def test_execute_and_recover_refused_when_execution_log_corrupt(self) -> None:
        plan = self._prepare()
        self.assertEqual(self._execute(plan).moved, 1)
        journal = self.sandbox.journal(plan["plan_id"], "execution")
        gen = Path(journal.generation_file(1))
        gen.write_bytes(b"{ broken\n" + gen.read_bytes())
        before = gen.read_bytes()

        with self.assertRaises(ExecutionRefused):
            self._execute(plan)
        report = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(report["phase"], "corrupted")
        self.assertEqual(gen.read_bytes(), before)

    def test_snapshot_not_replaced_when_corrupt(self) -> None:
        plan = self._prepare()
        self.assertEqual(self._execute(plan).moved, 1)
        self._corrupt_undo_log(plan)
        snapshot_file = Path(snapshot_path(self.sandbox.settings, plan["plan_id"]))
        before = snapshot_file.read_bytes()

        recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(snapshot_file.read_bytes(), before,
                         "损坏时不得替换快照")

    def test_global_execution_failure_persists_abnormal_phase(self) -> None:
        """复现主控反例 `execution_abnormal_phase`：终态不得为 executed。"""
        plan = self._prepare()

        def hook(stage: str, info: dict) -> None:
            if stage == "before_move":
                source = Path(info["source"])
                source.rename(self.sandbox.root / "preserved-original.pdf")
                source.write_bytes(b"new synthetic replacement")

        result = self._execute_with_hook(plan, hook)
        self.assertTrue(result.stopped)
        self.assertIn("MOVE_IDENTITY_MISMATCH", str(result.stop_reason))
        self.assertNotEqual(result.phase, "executed", "global 失败不得留下 executed")
        self.assertEqual(result.phase, "needs_review")
        self.assertEqual(current_phase(self.sandbox.settings, plan["plan_id"]), "needs_review",
                         "持久化阶段必须是异常态")

    def _execute_with_hook(self, plan: dict, hook):
        return execute_plan(
            self.sandbox.settings, plan["plan_id"], plan["plan_hash"], hook=hook
        )

    def test_undo_global_stop_persists_needs_review(self) -> None:
        for name in ("a.pdf", "b.pdf"):
            self.sandbox.add_file(name, b"PAYLOAD-" + name.encode())
        plan = self.sandbox.make_plan()
        self.assertEqual(self._execute(plan).moved, 2)
        first = plan["operations"][0]
        self.sandbox.journal(plan["plan_id"], "undo").append(make_event(
            "undo_skip", first["op_id"], undo_state="unknown_needs_review",
            reason_code="UNKNOWN_STATE", reason="synthetic global",
            expected_identity=first["identity"],
        ))
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertTrue(undo.stopped)
        self.assertEqual(undo.undo_status, "needs_review")
        self.assertEqual(current_phase(self.sandbox.settings, plan["plan_id"]), "needs_review")

    def test_healthy_logs_still_allow_execute_undo_recover(self) -> None:
        """不得以「一律拒绝」换取通过：健康日志必须照常闭环。"""
        plan = self._prepare()
        self.assertEqual(self._execute(plan).moved, 1)
        self.assertIn(recover_execution(self.sandbox.settings, plan["plan_id"])["phase"],
                      {"executed", "partial_undo", "needs_review", "undone"})
        self.assertEqual(undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True).restored, 1)

    def test_truncated_tail_still_recoverable(self) -> None:
        """代次截断恢复协议仍须通过（准入不得把可修复的截断当成损坏）。"""
        plan = self._prepare()
        op = plan["operations"][0]
        from doc_selecter.directory_archive import ensure_layout
        ensure_layout(self.sandbox.settings,plan)
        journal = self.sandbox.journal(plan["plan_id"], "execution")
        journal.append(make_event(
            "intent", op["op_id"], source=op["source"], dest=op["dest"],
            category=op["category"], identity=op["identity"],
        ))
        gen = Path(journal.generation_file(1))
        with open(gen, "ab") as fh:
            fh.write(b'{"event":"intent","op_id":"op-0002"')
        before = gen.read_bytes()

        report = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertNotEqual(report["phase"], "corrupted", report.get("notes"))
        self.assertEqual(gen.read_bytes(), before, "封存不得改动旧代次字节")


class UndoLockOrderTests(_Base):
    """4：撤销必须持锁后才校验/准入/探测；锁冲突时不得探测。"""

    def test_undo_lock_busy_does_not_probe(self) -> None:
        """复现主控反例 `undo_probe_before_lock`。"""
        plan = self._prepare()
        self.assertEqual(self._execute(plan).moved, 1)

        lock = GlobalLock(self.sandbox.settings)
        handle = lock.acquire(kind="cli", action="undo", plan_id="holder")
        calls: list[str] = []
        real_probe = undo_module.probe_capability

        def observed(settings):
            calls.append("probe")
            return real_probe(settings)

        refused = False
        try:
            with patch.object(undo_module, "probe_capability", observed):
                try:
                    undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
                except (UndoRefused, LockBusy):
                    refused = True
        finally:
            lock.release(handle)

        self.assertTrue(refused, "持锁期间撤销必须被拒绝")
        self.assertEqual(calls, [], "锁冲突时绝不允许调用能力探测")

    def test_undo_probes_after_lock_and_admission(self) -> None:
        """正常路径：探测发生在取锁与准入之后（顺序断言）。"""
        plan = self._prepare()
        self.assertEqual(self._execute(plan).moved, 1)

        order: list[str] = []
        real_probe = undo_module.probe_capability
        real_verify = undo_module.load_verified_plan
        real_admit = undo_module.assert_journals_admissible

        def spy_verify(settings, plan_id, **kwargs):
            order.append("verify_binding")
            self.assertTrue(kwargs.get("verify_binding"), "撤销入口必须启用绑定校验")
            return real_verify(settings, plan_id, **kwargs)

        def spy_admit(settings, plan_id, **kwargs):
            order.append("admission")
            return real_admit(settings, plan_id, **kwargs)

        def spy_probe(settings):
            order.append("probe")
            return real_probe(settings)

        with patch.object(undo_module, "load_verified_plan", spy_verify), \
             patch.object(undo_module, "assert_journals_admissible", spy_admit), \
             patch.object(undo_module, "probe_capability", spy_probe):
            undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)

        self.assertEqual(undo.restored, 1, undo.entries)
        self.assertEqual(order, ["verify_binding", "admission", "probe"],
                         f"顺序必须是 绑定 → 准入 → 探测；实际 {order}")

    def test_undo_binding_failure_happens_before_probe(self) -> None:
        plan = self._prepare()
        self.assertEqual(self._execute(plan).moved, 1)
        other = self._alternate_settings()
        calls: list[str] = []
        real_probe = undo_module.probe_capability

        def spy(settings):
            calls.append("probe")
            return real_probe(settings)

        with patch.object(undo_module, "probe_capability", spy):
            with self.assertRaises(UndoRefused):
                undo_plan(other, plan["plan_id"], confirm=True)
        self.assertEqual(calls, [], "绑定失败时不得探测")

    def test_execute_lock_busy_does_not_probe(self) -> None:
        """执行入口同样：锁冲突时不得探测。"""
        plan = self._prepare()
        lock = GlobalLock(self.sandbox.settings)
        handle = lock.acquire(kind="cli", action="execute", plan_id="holder")
        calls: list[str] = []
        from doc_selecter import executor as executor_module

        real_probe = executor_module.probe_capability

        def spy(settings):
            calls.append("probe")
            return real_probe(settings)

        try:
            with patch.object(executor_module, "probe_capability", spy):
                with self.assertRaises((ExecutionRefused, LockBusy)):
                    self._execute(plan)
        finally:
            lock.release(handle)
        self.assertEqual(calls, [], "执行锁冲突时不得探测")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
