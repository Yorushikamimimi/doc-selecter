"""第三轮：**公开恢复入口**与只读状态识别的回归。

对应主控第二轮复验的四个反例（`recovery_entry_checks.py` /
`recovery-entry-results.json`）：

1. ``missing_manifest``：单代次清单缺失 → 公开 ``recover`` 必须重建清单、幂等、旧字节不变
2. ``pending_empty_generation``：合法截断 + 未登记空 N+1 → 必须完成发布
3. ``readonly_creates_directory``：只读校验不得创建目录
4. ``probe_accepts_corrupt_missing_manifest``：伪 repairable（缺失清单 + 内容损坏）必须判 corrupted

要求：这些用例都经过**公开 recover 入口**（至少一项经 CLI），不直接调用底层
``ensure_sealed``；并覆盖「合法可修复 + 另一日志损坏 → 零修复/封存/补写/快照替换」。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.harness import SandboxTestCase

from doc_selecter.executor import (
    ExecutionRefused,
    classify_journals,
    execute_plan,
    recover_execution,
)
from doc_selecter.journal import (
    JOURNAL_CORRUPTED,
    JOURNAL_HEALTHY,
    JOURNAL_REPAIRABLE,
    Journal,
    JournalCorrupted,
    JournalNeedsRepair,
    make_event,
)
from doc_selecter.snapshot import read_snapshot, snapshot_path
from doc_selecter.undo import UndoRefused, undo_plan

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RecoveryContractBase(SandboxTestCase):
    def _executed(self, name: str = "report.pdf") -> dict:
        self.sandbox.add_file(name, b"owned synthetic payload")
        plan = self.sandbox.make_plan()
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 1, result.outcomes)
        return plan

    def _journal(self, plan: dict, kind: str = "execution") -> Journal:
        return self.sandbox.journal(plan["plan_id"], kind)

    def _cli(self, *argv: str) -> tuple[int, str]:
        from doc_selecter.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            code = main(list(argv))
        return code, buf.getvalue()

    def _truncate_tail(self, journal: Journal) -> bytes:
        gen = Path(journal.generation_file(1))
        with open(gen, "ab") as fh:
            fh.write(b'{"event":"intent","op_id":"op-9999"')
        return gen.read_bytes()

    def _corrupt_middle(self, journal: Journal) -> bytes:
        gen = Path(journal.generation_file(1))
        gen.write_bytes(b"not-json\n" + gen.read_bytes())
        return gen.read_bytes()


class MissingManifestRecoveryTests(RecoveryContractBase):
    """单代次清单缺失：公开 recover 重建 + 幂等 + 之后仍能安全撤销。"""

    def test_public_recover_rebuilds_single_generation_manifest(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)
        gen = Path(journal.generation_file(1))
        before = gen.read_bytes()
        Path(journal.manifest_path).unlink()

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertNotEqual(report["phase"], "corrupted", report["notes"])
        self.assertTrue(Path(journal.manifest_path).exists(), "公开 recover 必须重建清单")
        self.assertEqual(gen.read_bytes(), before, "旧代次字节必须原样保留")
        state = journal.classify()
        self.assertEqual(state.classification, JOURNAL_HEALTHY, state.detail)
        self.assertTrue(any("重建" in n for n in report["notes"]), report["notes"])

    def test_public_recover_is_idempotent(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)
        gen = Path(journal.generation_file(1))
        Path(journal.manifest_path).unlink()

        first = recover_execution(self.sandbox.settings, plan["plan_id"])
        manifest_after_first = Path(journal.manifest_path).read_bytes()
        before = gen.read_bytes()
        second = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertNotEqual(first["phase"], "corrupted")
        self.assertNotEqual(second["phase"], "corrupted")
        self.assertEqual(Path(journal.manifest_path).read_bytes(), manifest_after_first,
                         "第二次恢复不得不必要地改写清单")
        self.assertEqual(gen.read_bytes(), before)
        self.assertEqual(journal.classify().classification, JOURNAL_HEALTHY)

    def test_cli_recover_repairs_then_undo_succeeds(self) -> None:
        """至少一项经 CLI 的公开恢复；修复后必须仍能安全撤销。"""
        plan = self._executed()
        journal = self._journal(plan)
        gen = Path(journal.generation_file(1))
        before = gen.read_bytes()
        Path(journal.manifest_path).unlink()

        code, out = self._cli("recover", "--plan", plan["plan_id"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("corrupted", out)
        self.assertTrue(Path(journal.manifest_path).exists(), out)
        self.assertEqual(gen.read_bytes(), before)

        code, out = self._cli("recover", "--plan", plan["plan_id"])
        self.assertEqual(code, 0, out)

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 1, undo.entries)
        self.assertExists(self.sandbox.downloads / "report.pdf")

    def test_execute_and_undo_refuse_and_point_to_recover(self) -> None:
        """未修复时 execute/undo 必须明确拒绝并提示 recover。"""
        plan = self._executed()
        journal = self._journal(plan)
        Path(journal.manifest_path).unlink()

        with self.assertRaises(ExecutionRefused) as ctx:
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertIn("recover", str(ctx.exception))
        with self.assertRaises(UndoRefused) as ctx2:
            undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertIn("recover", str(ctx2.exception))
        # 拒绝过程中不得修复
        self.assertFalse(Path(journal.manifest_path).exists())


class PendingGenerationRecoveryTests(RecoveryContractBase):
    """空 N+1 发布中断（含合法截断）：公开 recover 完成发布 + 幂等。"""

    def test_public_recover_completes_interrupted_publish(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)
        gen1 = Path(journal.generation_file(1))
        before = self._truncate_tail(journal)
        manifest_before = Path(journal.manifest_path).read_bytes()
        gen2 = Path(journal.generation_file(2))
        gen2.touch(exist_ok=False)
        self.assertEqual(gen2.stat().st_size, 0)

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertNotEqual(report["phase"], "corrupted", report["notes"])
        self.assertEqual(gen1.read_bytes(), before, "旧代次字节必须原样保留")
        self.assertEqual(gen2.read_bytes(), b"", "空 N+1 代必须被复用而不是写入")
        self.assertNotEqual(Path(journal.manifest_path).read_bytes(), manifest_before,
                            "必须发布新清单")
        manifest = json.loads(Path(journal.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual([g["gen"] for g in manifest["generations"]], [1, 2])
        self.assertEqual(manifest["generations"][0]["status"], "sealed")
        self.assertEqual(manifest["generations"][1]["status"], "active")
        self.assertEqual(journal.classify().classification, JOURNAL_HEALTHY)

    def test_public_recover_publish_is_idempotent(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)
        self._truncate_tail(journal)
        Path(journal.generation_file(2)).touch(exist_ok=False)

        first = recover_execution(self.sandbox.settings, plan["plan_id"])
        manifest_after = Path(journal.manifest_path).read_bytes()
        second = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertNotEqual(first["phase"], "corrupted")
        self.assertNotEqual(second["phase"], "corrupted")
        self.assertEqual(Path(journal.manifest_path).read_bytes(), manifest_after,
                         "幂等：第二次恢复不得改写清单")
        self.assertEqual([g["gen"] for g in json.loads(
            Path(journal.manifest_path).read_text(encoding="utf-8"))["generations"]], [1, 2])

    def test_cli_recover_seals_and_undo_succeeds(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)
        self._truncate_tail(journal)
        Path(journal.generation_file(2)).touch(exist_ok=False)
        before = Path(journal.generation_file(1)).read_bytes()

        code, out = self._cli("recover", "--plan", plan["plan_id"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("corrupted", out)
        self.assertEqual(Path(journal.generation_file(1)).read_bytes(), before)

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 1, undo.entries)

    def test_execute_refused_while_repair_pending(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)
        self._truncate_tail(journal)
        Path(journal.generation_file(2)).touch(exist_ok=False)
        manifest_before = Path(journal.manifest_path).read_bytes()

        with self.assertRaises(ExecutionRefused) as ctx:
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertIn("recover", str(ctx.exception))
        self.assertEqual(Path(journal.manifest_path).read_bytes(), manifest_before,
                         "拒绝时不得修复")


class UndoJournalRecoveryTests(RecoveryContractBase):
    """撤销日志侧的同样两种可修复状态（覆盖另一种日志）。"""

    def _undo_journal(self, plan: dict) -> Journal:
        journal = self._journal(plan, "undo")
        # 先产生一条合法撤销事件，使撤销日志存在
        op = plan["operations"][0]
        journal.append(make_event(
            "undo_skip", op["op_id"], undo_state="unknown_needs_review",
            reason_code="UNKNOWN_STATE", reason="synthetic global",
            expected_identity=op["identity"],
        ))
        return journal

    def test_undo_log_missing_manifest_via_public_recover(self) -> None:
        plan = self._executed()
        journal = self._undo_journal(plan)
        gen = Path(journal.generation_file(1))
        before = gen.read_bytes()
        Path(journal.manifest_path).unlink()

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertNotEqual(report["phase"], "corrupted", report["notes"])
        self.assertTrue(Path(journal.manifest_path).exists(), "撤销日志清单必须被重建")
        self.assertEqual(gen.read_bytes(), before)
        self.assertEqual(journal.classify().classification, JOURNAL_HEALTHY)

    def test_undo_log_pending_generation_via_public_recover(self) -> None:
        plan = self._executed()
        journal = self._undo_journal(plan)
        gen1 = Path(journal.generation_file(1))
        with open(gen1, "ab") as fh:
            fh.write(b'{"event":"undo_result"')
        before = gen1.read_bytes()
        Path(journal.generation_file(2)).touch(exist_ok=False)

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertNotEqual(report["phase"], "corrupted", report["notes"])
        self.assertEqual(gen1.read_bytes(), before)
        manifest = json.loads(Path(journal.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual([g["gen"] for g in manifest["generations"]], [1, 2])
        self.assertEqual(journal.classify().classification, JOURNAL_HEALTHY)


class ReadOnlyGuaranteeTests(RecoveryContractBase):
    """只读：不创建目录；权限/IO 错误不得当成不存在。"""

    def test_read_events_does_not_create_directory(self) -> None:
        """复现主控反例 `readonly_creates_directory`。"""
        journals = self.sandbox.state / "journals"
        undos = self.sandbox.state / "undos"
        self.assertFalse(journals.exists())
        self.assertFalse(undos.exists())

        for kind in ("execution", "undo"):
            journal = Journal(str(self.sandbox.state), "artificial-readonly", kind)
            events, _ = journal.read_events(allow_repair=False)
            self.assertEqual(events, [])
            state = journal.classify()
            self.assertEqual(state.classification, JOURNAL_HEALTHY, state.detail)
            probe = journal.probe_integrity()
            self.assertEqual(probe["classification"], JOURNAL_HEALTHY)

        self.assertFalse(journals.exists(), "只读校验不得创建 journals 目录")
        self.assertFalse(undos.exists(), "只读校验不得创建 undos 目录")

    def test_classify_does_not_create_directory(self) -> None:
        journal = Journal(str(self.sandbox.state), "artificial-readonly", "execution")
        journal.classify()
        journal.probe_integrity()
        self.assertFalse((self.sandbox.state / "journals").exists())

    def test_missing_state_dir_returns_empty_state(self) -> None:
        missing = self.sandbox.root / "no-such-state"
        journal = Journal(str(missing), "plan-x", "execution")
        state = journal.classify()
        self.assertEqual(state.classification, JOURNAL_HEALTHY, state.detail)
        events, _ = journal.read_events(allow_repair=False)
        self.assertEqual(events, [])
        self.assertFalse(missing.exists(), "只读校验不得创建状态目录")

    def test_permission_error_is_not_treated_as_missing(self) -> None:
        """目录不可读 → 必须报错（corrupted），不得当成「不存在」。"""
        journals = self.sandbox.state / "journals"
        journals.mkdir(parents=True, exist_ok=True)
        os.chmod(journals, 0o000)
        self.addCleanup(os.chmod, journals, 0o700)
        journal = Journal(str(self.sandbox.state), "plan-x", "execution")
        try:
            state = journal.classify()
        except JournalCorrupted:
            return  # 抛错同样是「不当成不存在」
        self.assertEqual(state.classification, JOURNAL_CORRUPTED, state.detail)


class PseudoRepairableTests(RecoveryContractBase):
    """伪 repairable 负例：缺失清单但内容损坏 / 多代次 / 非法新代次 → corrupted。"""

    def test_missing_manifest_but_corrupt_content_is_corrupted(self) -> None:
        """复现主控反例 `probe_accepts_corrupt_missing_manifest`。"""
        plan = self._executed()
        journal = self._journal(plan)
        gen = Path(journal.generation_file(1))
        before = self._corrupt_middle(journal)
        Path(journal.manifest_path).unlink()

        probe = journal.probe_integrity()
        self.assertFalse(probe["ok"], f"伪 repairable 不得判 ok：{probe}")
        self.assertEqual(probe["classification"], JOURNAL_CORRUPTED)
        self.assertFalse(probe["needs_repair"])
        self.assertEqual(gen.read_bytes(), before, "探测不得改动字节")

        report = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(report["phase"], "corrupted")
        self.assertFalse(Path(journal.manifest_path).exists(), "损坏时不得重建清单")
        self.assertEqual(gen.read_bytes(), before)

    def test_missing_manifest_multiple_generations_is_corrupted(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)
        Path(journal.generation_file(2)).write_bytes(b"")
        Path(journal.manifest_path).unlink()

        self.assertEqual(journal.classify().classification, JOURNAL_CORRUPTED)
        report = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(report["phase"], "corrupted")

    def test_unregistered_nonempty_generation_is_corrupted(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)
        Path(journal.generation_file(2)).write_bytes(
            b'{"event":"intent","op_id":"x"}\n'
        )
        self.assertEqual(journal.classify().classification, JOURNAL_CORRUPTED)
        report = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(report["phase"], "corrupted")

    def test_unregistered_wrong_number_is_corrupted(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)
        Path(journal.generation_file(5)).write_bytes(b"")
        self.assertEqual(journal.classify().classification, JOURNAL_CORRUPTED)

    def test_crc_mismatch_is_corrupted(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)
        gen = Path(journal.generation_file(1))
        record = json.loads(gen.read_text(encoding="utf-8").splitlines()[0])
        record["crc32"] = "00000000"
        gen.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        Path(journal.manifest_path).unlink()
        self.assertEqual(journal.classify().classification, JOURNAL_CORRUPTED)

    def test_repairable_classification_requires_valid_content(self) -> None:
        """对照：内容合法的单代次缺失清单才是 repairable。"""
        plan = self._executed()
        journal = self._journal(plan)
        Path(journal.manifest_path).unlink()
        state = journal.classify()
        self.assertEqual(state.classification, JOURNAL_REPAIRABLE)
        self.assertEqual(state.repair_kind, "rebuild_manifest_single_generation")


class TwoJournalOrderTests(RecoveryContractBase):
    """合法可修复 + 另一份损坏 → 零修复、零封存、零补写、零快照替换。"""

    def test_repairable_execution_plus_corrupt_undo_writes_nothing(self) -> None:
        plan = self._executed()
        exec_journal = self._journal(plan)
        undo_journal = self._journal(plan, "undo")
        op = plan["operations"][0]
        undo_journal.append(make_event(
            "undo_skip", op["op_id"], undo_state="unknown_needs_review",
            reason_code="UNKNOWN_STATE", reason="synthetic",
            expected_identity=op["identity"],
        ))
        # 执行日志：可修复（清单缺失）；撤销日志：中间损坏
        Path(exec_journal.manifest_path).unlink()
        undo_gen = Path(undo_journal.generation_file(1))
        undo_before = self._corrupt_middle(undo_journal)
        exec_gen = Path(exec_journal.generation_file(1))
        exec_before = exec_gen.read_bytes()
        snapshot_file = Path(snapshot_path(self.sandbox.settings, plan["plan_id"]))
        snapshot_before = snapshot_file.read_bytes() if snapshot_file.exists() else None

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertEqual(report["phase"], "corrupted")
        self.assertFalse(Path(exec_journal.manifest_path).exists(),
                         "另一份损坏时不得修复可修复的那份")
        self.assertEqual(exec_gen.read_bytes(), exec_before, "执行日志字节必须不变")
        self.assertEqual(undo_gen.read_bytes(), undo_before, "撤销日志字节必须不变")
        if snapshot_before is None:
            self.assertFalse(snapshot_file.exists(), "不得新建快照")
        else:
            self.assertEqual(snapshot_file.read_bytes(), snapshot_before, "不得替换快照")
        self.assertEqual(report["recovered"], [], "不得补写任何正常事件")

    def test_repairable_undo_plus_corrupt_execution_writes_nothing(self) -> None:
        plan = self._executed()
        exec_journal = self._journal(plan)
        undo_journal = self._journal(plan, "undo")
        op = plan["operations"][0]
        undo_journal.append(make_event(
            "undo_skip", op["op_id"], undo_state="unknown_needs_review",
            reason_code="UNKNOWN_STATE", reason="synthetic", expected_identity=op["identity"],
        ))
        Path(undo_journal.manifest_path).unlink()
        exec_gen = Path(exec_journal.generation_file(1))
        exec_before = self._corrupt_middle(exec_journal)
        undo_gen = Path(undo_journal.generation_file(1))
        undo_before = undo_gen.read_bytes()

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertEqual(report["phase"], "corrupted")
        self.assertFalse(Path(undo_journal.manifest_path).exists())
        self.assertEqual(exec_gen.read_bytes(), exec_before)
        self.assertEqual(undo_gen.read_bytes(), undo_before)

    def test_both_repairable_are_both_repaired(self) -> None:
        """两份都可修复时，公开 recover 必须把两份都修好（不得只修第一份）。"""
        plan = self._executed()
        exec_journal = self._journal(plan)
        undo_journal = self._journal(plan, "undo")
        op = plan["operations"][0]
        undo_journal.append(make_event(
            "undo_skip", op["op_id"], undo_state="unknown_needs_review",
            reason_code="UNKNOWN_STATE", reason="synthetic", expected_identity=op["identity"],
        ))
        Path(exec_journal.manifest_path).unlink()
        Path(undo_journal.manifest_path).unlink()

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertNotEqual(report["phase"], "corrupted", report["notes"])
        self.assertTrue(Path(exec_journal.manifest_path).exists())
        self.assertTrue(Path(undo_journal.manifest_path).exists())
        self.assertEqual(exec_journal.classify().classification, JOURNAL_HEALTHY)
        self.assertEqual(undo_journal.classify().classification, JOURNAL_HEALTHY)


class ClassificationApiTests(RecoveryContractBase):
    """结构化分类 API：三种状态严格区分，且不依赖异常文本。"""

    def test_classify_distinguishes_three_states(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)

        self.assertEqual(journal.classify().classification, JOURNAL_HEALTHY)
        Path(journal.manifest_path).unlink()
        self.assertEqual(journal.classify().classification, JOURNAL_REPAIRABLE)
        Path(journal.generation_file(1)).write_bytes(b"garbage\n")
        self.assertEqual(journal.classify().classification, JOURNAL_CORRUPTED)

    def test_needs_repair_exception_carries_state(self) -> None:
        plan = self._executed()
        journal = self._journal(plan)
        Path(journal.manifest_path).unlink()
        with self.assertRaises(JournalNeedsRepair) as ctx:
            journal.read_events(allow_repair=False)
        self.assertIsNotNone(ctx.exception.state)
        self.assertEqual(ctx.exception.state.classification, JOURNAL_REPAIRABLE)
        self.assertEqual(ctx.exception.state.repair_kind, "rebuild_manifest_single_generation")

    def test_classify_journals_reports_both(self) -> None:
        plan = self._executed()
        Path(self._journal(plan).manifest_path).unlink()
        classified = classify_journals(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(set(classified), {"execution", "undo"})
        self.assertEqual(classified["execution"]["state"].classification, JOURNAL_REPAIRABLE)
        self.assertEqual(classified["undo"]["state"].classification, JOURNAL_HEALTHY)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
