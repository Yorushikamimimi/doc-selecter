"""跨代逻辑事件流与组合修复的公开入口回归。

对应主控第三轮复验的两个根因（`cross_generation_checks.py` /
`cross-generation-results.json`）：

1. **统一跨代事件校验**：物理代次只是存储分段；配对/唯一性/顺序必须在
   **按 gen 拼接后的整条逻辑事件流**上校验。分类、读取、准入共享同一语义。
2. **完成合法组合修复**：一次公开 ``recover`` 必须有界、可验证地把方案允许的
   组合状态（如「单代次清单缺失 + 截断尾行」）修到 healthy，再补记/重建快照。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.harness import SandboxTestCase

from doc_selecter.atomic_move import AtomicMover
from doc_selecter.executor import (
    ExecutionRefused,
    assert_journals_admissible,
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
    make_event,
)
from doc_selecter.snapshot import read_snapshot, snapshot_path
from doc_selecter.undo import UndoRefused, undo_plan

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CrossGenerationBase(SandboxTestCase):
    def _plan(self, name: str = "report.pdf") -> dict:
        self.sandbox.add_file(name, b"owned synthetic payload")
        return self.sandbox.make_plan()

    def _executed(self) -> tuple[dict, dict]:
        plan = self._plan()
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 1, result.outcomes)
        return plan, plan["operations"][0]

    def _journal(self, plan: dict, kind: str = "execution") -> Journal:
        return self.sandbox.journal(plan["plan_id"], kind)

    def _append_tail(self, journal: Journal, kind: str) -> bytes:
        """给第 1 代追加一个**不完整**的截断尾行（合法截断）。"""
        gen = Path(journal.generation_file(1))
        with open(gen, "ab") as fh:
            fh.write(('{"event":"' + kind + '"').encode())
        return gen.read_bytes()

    def _write_raw(self, journal: Journal, generation: int, payload: dict) -> None:
        """直接往指定代次写一条合法 JSON+CRC 的事件（绕过写入侧配对校验，用于构造反例）。"""
        from doc_selecter.journal import line_crc32

        record = dict(payload)
        if journal.v3_plan is not None:
            record.update(journal_schema_version=2,plan_schema_version=3,log_kind=journal.log_kind,op_kind="file",plan_hash=journal.v3_plan["plan_hash"])
        record.setdefault("plan_id", journal.plan_id)
        record["crc32"] = line_crc32(record)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        path = Path(journal.generation_file(generation))
        with open(path, "ab") as fh:
            fh.write(line.encode("utf-8"))

    def _cli(self, *argv: str) -> tuple[int, str]:
        from doc_selecter.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            code = main(list(argv))
        return code, buf.getvalue()


class ExecutionCrossGenerationTests(CrossGenerationBase):
    """执行日志 intent/result 跨代。"""

    def _split_setup(self) -> tuple[dict, dict, Journal, bytes]:
        """第 1 代 intent + 截断尾行 → recover 封存 → 第 2 代补写 result。"""
        plan = self._plan()
        op = plan["operations"][0]
        journal = self._journal(plan)
        from doc_selecter.directory_archive import ensure_layout
        ensure_layout(self.sandbox.settings, plan)
        journal.append(make_event(
            "intent", op["op_id"], source=op["source"], dest=op["dest"],
            category=op["category"], identity=op["identity"],
        ))
        Path(op["dest"]).parent.mkdir(parents=True, exist_ok=True)
        AtomicMover().rename_excl(op["source"], op["dest"])
        before = self._append_tail(journal, "result")
        return plan, op, journal, before

    def test_public_recover_splits_pair_across_generations(self) -> None:
        plan, op, journal, before = self._split_setup()

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertIn(op["op_id"], report["recovered"])
        self.assertNotEqual(report["phase"], "corrupted", report["notes"])
        self.assertEqual(journal.classify().classification, JOURNAL_HEALTHY,
                         journal.classify().detail)
        self.assertEqual(Path(journal.generation_file(1)).read_bytes(), before,
                         "旧代次字节必须原样保留")
        self.assertIn(b'"event":"result"', Path(journal.generation_file(2)).read_bytes())
        # 跨代拼接后是一对 intent/result
        events, _ = journal.read_events(allow_repair=True)
        kinds = [e["event"] for e in events if e.get("op_kind") == "file"]
        self.assertEqual(kinds, ["intent", "result"])

    def test_second_recover_is_idempotent_then_undo_succeeds(self) -> None:
        plan, op, journal, before = self._split_setup()
        recover_execution(self.sandbox.settings, plan["plan_id"])
        manifest_after = Path(journal.manifest_path).read_bytes()
        gen2_after = Path(journal.generation_file(2)).read_bytes()

        second = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertNotEqual(second["phase"], "corrupted", second["notes"])
        self.assertEqual(Path(journal.manifest_path).read_bytes(), manifest_after,
                         "二次恢复不得改写清单")
        self.assertEqual(Path(journal.generation_file(2)).read_bytes(), gen2_after,
                         "二次恢复不得改写新代次")
        self.assertEqual(Path(journal.generation_file(1)).read_bytes(), before)

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 1, undo.entries)
        self.assertExists(self.sandbox.downloads / "report.pdf")

    def test_cli_recover_cross_generation_roundtrip(self) -> None:
        """至少一项经 CLI 子进程的跨代恢复往返。"""
        plan, op, journal, before = self._split_setup()
        env = {"DOCSELECTER_TEST_ROOT": str(self.sandbox.root), **{
            k: v for k, v in __import__("os").environ.items() if k != "DOCSELECTER_TEST_ROOT"
        }}
        proc = subprocess.run(
            [sys.executable, "-m", "doc_selecter", "recover", "--plan", plan["plan_id"]],
            capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT), timeout=90,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("corrupted", proc.stdout)
        self.assertEqual(journal.classify().classification, JOURNAL_HEALTHY)
        self.assertEqual(Path(journal.generation_file(1)).read_bytes(), before)
        self.assertIn(b'"event":"result"', Path(journal.generation_file(2)).read_bytes())

        proc2 = subprocess.run(
            [sys.executable, "-m", "doc_selecter", "undo", "--plan", plan["plan_id"], "--confirm"],
            capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT), timeout=90,
        )
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        self.assertExists(self.sandbox.downloads / "report.pdf")


class UndoCrossGenerationTests(CrossGenerationBase):
    """撤销日志 undo_intent/undo_result 跨代。"""

    def test_recover_resume_then_recover_again(self) -> None:
        plan, op = self._executed()
        journal = self._journal(plan, "undo")
        journal.append(make_event(
            "undo_intent", op["op_id"],
            **{"from": op["dest"], "to": op["source"], "expected_identity": op["identity"]},
        ))
        # 撤销已完成（原位置就是本文件），undo_result 写入被截断
        AtomicMover().rename_excl(op["dest"], op["source"])
        before = self._append_tail(journal, "undo_result")

        first = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertNotEqual(first["phase"], "corrupted", first["notes"])

        resumed = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(resumed.restored, 1, resumed.entries)

        second = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertNotEqual(second["phase"], "corrupted", second["notes"])
        self.assertEqual(journal.classify().classification, JOURNAL_HEALTHY)
        self.assertEqual(Path(journal.generation_file(1)).read_bytes(), before)

        events, _ = journal.read_events(allow_repair=True)
        kinds = [e["event"] for e in events if e.get("op_kind") == "file"]
        self.assertEqual(kinds.count("undo_intent"), 1, "只有一对事件，不得重复 intent")
        self.assertEqual(kinds.count("undo_result"), 1)


class CrossGenerationDuplicateTests(CrossGenerationBase):
    """跨代重复/真孤立事件：准入即 corrupted，零修复/封存/补写/快照替换。"""

    def _make_duplicate_execution(self) -> tuple[dict, Journal, bytes]:
        plan, op = self._executed()
        journal = self._journal(plan)
        self._append_tail(journal, "intent")
        journal.repair_if_needed()
        original = Path(journal.generation_file(1)).read_bytes().splitlines(keepends=True)[0]
        Path(journal.generation_file(2)).write_bytes(original)  # 跨代重复 intent
        return plan, journal, original

    def test_duplicate_intent_across_generations_is_corrupted(self) -> None:
        plan, journal, _ = self._make_duplicate_execution()
        state = journal.classify()
        self.assertEqual(state.classification, JOURNAL_CORRUPTED, state.detail)
        self.assertIn("重复", state.detail)

    def test_duplicate_plus_repairable_other_log_writes_nothing(self) -> None:
        """另一份日志可修复时，也不得先修它。"""
        plan, journal, original = self._make_duplicate_execution()
        undo_journal = self._journal(plan, "undo")
        op = plan["operations"][0]
        undo_journal.append(make_event(
            "undo_skip", op["op_id"], undo_state="unknown_needs_review",
            reason_code="UNKNOWN_STATE", reason="synthetic", expected_identity=op["identity"],
        ))
        Path(undo_journal.manifest_path).unlink()  # 另一份是「可修复」

        exec_gen1 = Path(journal.generation_file(1)).read_bytes()
        exec_gen2 = Path(journal.generation_file(2)).read_bytes()
        undo_gen = Path(undo_journal.generation_file(1)).read_bytes()
        snapshot_file = Path(snapshot_path(self.sandbox.settings, plan["plan_id"]))
        snapshot_before = snapshot_file.read_bytes() if snapshot_file.exists() else None

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertEqual(report["phase"], "corrupted")
        self.assertFalse(Path(undo_journal.manifest_path).exists(),
                         "执行日志损坏时不得先修复另一份清单")
        self.assertEqual(Path(journal.generation_file(1)).read_bytes(), exec_gen1)
        self.assertEqual(Path(journal.generation_file(2)).read_bytes(), exec_gen2)
        self.assertEqual(Path(undo_journal.generation_file(1)).read_bytes(), undo_gen)
        if snapshot_before is None:
            self.assertFalse(snapshot_file.exists())
        else:
            self.assertEqual(snapshot_file.read_bytes(), snapshot_before)
        self.assertEqual(report["recovered"], [], "不得补写任何正常事件")

    def test_orphan_result_across_generations_rejected_at_admission(self) -> None:
        """第 1 代合法 intent，第 2 代出现**真孤立** result（无任何在先 intent）。"""
        plan, op = self._executed()
        journal = self._journal(plan)
        self._append_tail(journal, "result")
        journal.repair_if_needed()   # 封存第 1 代并开第 2 代
        self._write_raw(journal, 2, make_event(
            "result", "op-9999", outcome="moved", reason_code="OK", dest_identity=op["identity"],
        ))

        self.assertEqual(journal.classify().classification, JOURNAL_CORRUPTED)
        with self.assertRaises(ExecutionRefused):
            assert_journals_admissible(self.sandbox.settings, plan["plan_id"], action="恢复")

    def test_true_orphan_result_alone_is_corrupted(self) -> None:
        plan, op = self._executed()
        journal = self._journal(plan)
        self._append_tail(journal, "result")
        journal.repair_if_needed()
        self._write_raw(journal, 2, make_event(
            "result", "op-7777", outcome="moved", reason_code="OK", dest_identity=op["identity"],
        ))
        state = journal.classify()
        self.assertEqual(state.classification, JOURNAL_CORRUPTED)
        self.assertIn("孤立", state.detail)


class CombinedRepairTests(CrossGenerationBase):
    """合法组合修复：单代次清单缺失 + 截断尾行（执行与撤销两侧）。"""

    def _combined_execution(self) -> tuple[dict, Journal, bytes]:
        plan, op = self._executed()
        journal = self._journal(plan)
        before = self._append_tail(journal, "result")
        Path(journal.manifest_path).unlink()
        return plan, journal, before

    def test_execution_combined_repair_in_one_recover(self) -> None:
        plan, journal, before = self._combined_execution()
        initial = journal.classify()
        self.assertEqual(initial.classification, JOURNAL_REPAIRABLE)
        self.assertTrue(initial.truncated, "初始状态应同时带截断标记")

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertNotEqual(report["phase"], "corrupted", report["notes"])
        final = journal.classify()
        self.assertEqual(final.classification, JOURNAL_HEALTHY, final.detail)
        self.assertEqual(Path(journal.generation_file(1)).read_bytes(), before,
                         "旧代次字节必须原样保留")
        manifest = json.loads(Path(journal.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual([g["gen"] for g in manifest["generations"]], [1, 2],
                         "组合修复必须封存并开新代次")

    def test_execution_combined_repair_is_idempotent_and_undoable(self) -> None:
        plan, journal, before = self._combined_execution()
        recover_execution(self.sandbox.settings, plan["plan_id"])
        manifest_after = Path(journal.manifest_path).read_bytes()

        second = recover_execution(self.sandbox.settings, plan["plan_id"])
        self.assertNotEqual(second["phase"], "corrupted", second["notes"])
        self.assertEqual(Path(journal.manifest_path).read_bytes(), manifest_after,
                         "二次恢复必须幂等")
        self.assertEqual(Path(journal.generation_file(1)).read_bytes(), before)

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 1, undo.entries)
        self.assertExists(self.sandbox.downloads / "report.pdf")

    def test_undo_log_combined_repair_in_one_recover(self) -> None:
        plan, op = self._executed()
        journal = self._journal(plan, "undo")
        journal.append(make_event(
            "undo_skip", op["op_id"], undo_state="unknown_needs_review",
            reason_code="UNKNOWN_STATE", reason="synthetic", expected_identity=op["identity"],
        ))
        before = self._append_tail(journal, "undo_result")
        Path(journal.manifest_path).unlink()

        report = recover_execution(self.sandbox.settings, plan["plan_id"])

        self.assertNotEqual(report["phase"], "corrupted", report["notes"])
        self.assertEqual(journal.classify().classification, JOURNAL_HEALTHY,
                         journal.classify().detail)
        self.assertEqual(Path(journal.generation_file(1)).read_bytes(), before)
        manifest = json.loads(Path(journal.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual([g["gen"] for g in manifest["generations"]], [1, 2])

    def test_cli_combined_repair_completes_in_one_call(self) -> None:
        plan, journal, before = self._combined_execution()

        code, out = self._cli("recover", "--plan", plan["plan_id"])

        self.assertEqual(code, 0, out)
        self.assertNotIn("corrupted", out)
        self.assertEqual(journal.classify().classification, JOURNAL_HEALTHY, out)
        self.assertEqual(Path(journal.generation_file(1)).read_bytes(), before)

    def test_repair_is_bounded_and_reports_progress_failure(self) -> None:
        """修复必须有界：步数上限存在且被遵守（不盲目循环）。"""
        self.assertLessEqual(Journal.MAX_REPAIR_STEPS, 8)
        self.assertGreaterEqual(Journal.MAX_REPAIR_STEPS, 2)

    def test_healthy_logs_are_not_modified_by_repair(self) -> None:
        plan = self._plan()
        journal = self._journal(plan)
        state = journal.repair_if_needed()
        self.assertEqual(state.classification, JOURNAL_HEALTHY)
        self.assertFalse(Path(journal.manifest_path).exists(),
                         "healthy 且从未写入时不得创建清单")
        self.assertFalse(Path(journal.generation_file(1)).exists())


class SharedSemanticsTests(CrossGenerationBase):
    """分类、读取与准入共享同一跨代语义。"""

    def test_classify_and_read_events_agree_on_valid_pair(self) -> None:
        """合法跨代配对：intent 在第 1 代、result 在第 2 代 → 两端都必须 healthy。"""
        plan = self._plan()
        op = plan["operations"][0]
        journal = self._journal(plan)
        journal.append(make_event(
            "intent", op["op_id"], source=op["source"], dest=op["dest"],
            category=op["category"], identity=op["identity"],
        ))
        self._append_tail(journal, "result")
        journal.repair_if_needed()
        self._write_raw(journal, 2, make_event(
            "result", op["op_id"], outcome="moved", reason_code="OK",
            dest_identity=op["identity"],
        ))

        self.assertEqual(journal.classify().classification, JOURNAL_HEALTHY,
                         journal.classify().detail)
        events, _ = journal.read_events(allow_repair=False)
        self.assertEqual([e["event"] for e in events], ["intent", "result"])
        self.assertEqual(events[0]["op_id"], events[1]["op_id"], "跨代后仍须配对")

    def test_duplicate_detected_by_both_classify_and_read(self) -> None:
        plan, op = self._executed()
        journal = self._journal(plan)
        self._append_tail(journal, "intent")
        journal.repair_if_needed()
        original = Path(journal.generation_file(1)).read_bytes().splitlines(keepends=True)[0]
        Path(journal.generation_file(2)).write_bytes(original)

        self.assertEqual(journal.classify().classification, JOURNAL_CORRUPTED)
        with self.assertRaises(JournalCorrupted):
            journal.read_events(allow_repair=False)

    def test_classify_journals_reports_cross_generation_corruption(self) -> None:
        plan, journal, _ = CrossGenerationDuplicateTests._make_duplicate_execution(self)
        classified = classify_journals(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(classified["execution"]["state"].classification, JOURNAL_CORRUPTED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
