"""撤销身份、重建基准与锁的测试（S6 / S13 / S16 / S17 + v3.1 收口断言）。"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

from tests.harness import SandboxTestCase

from doc_selecter import undo as undo_mod
from doc_selecter.atomic_move import lstat_identity
from doc_selecter.executor import execute_plan
from doc_selecter.journal import make_event
from doc_selecter.locking import GlobalLock, LockBusy, current_phase, read_plan_state
from doc_selecter.planner import build_plan, write_plan
from doc_selecter.snapshot import (
    read_snapshot,
    snapshot_path,
    write_snapshot,
)
from doc_selecter.undo import decide_attempting, undo_plan


class UndoIdentityTests(SandboxTestCase):
    """S6 / S16：原位置冲突与「绝不误报 restored」。"""

    def _execute_two(self) -> tuple[dict, dict]:
        self.sandbox.add_file("a.pdf", b"CONTENT-A")
        self.sandbox.add_file("b.pdf", b"CONTENT-B")
        plan = self.sandbox.make_plan()
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, 2, result.outcomes)
        return plan, result

    def test_s6_occupied_source_is_local_skip_and_never_overwritten(self) -> None:
        plan, _ = self._execute_two()
        # 在 a.pdf 的原位置放入一个不同内容的新文件
        intruder = self.sandbox.downloads / "a.pdf"
        intruder.write_bytes(b"USER-NEW-FILE")
        os.utime(intruder, ns=(555555, 555555))
        before = (intruder.read_bytes(), os.lstat(intruder).st_mtime_ns)

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)

        states = {e["op_id"]: e["undo_state"] for e in undo.entries}
        a_op = next(op["op_id"] for op in plan["operations"] if op["source"].endswith("a.pdf"))
        b_op = next(op["op_id"] for op in plan["operations"] if op["source"].endswith("b.pdf"))
        self.assertEqual(states[a_op], "blocked_source_occupied")
        self.assertEqual(states[b_op], "restored", "局部冲突必须跳过该项并继续还原其他项")
        after = (intruder.read_bytes(), os.lstat(intruder).st_mtime_ns)
        self.assertEqual(before, after, "用户放的文件绝不能被覆盖")
        self.assertEqual(undo.undo_status, "partial_undo")

    def test_s16_dest_removed_and_different_file_at_source_never_reports_restored(self) -> None:
        plan, _ = self._execute_two()
        a_op = next(op for op in plan["operations"] if op["source"].endswith("a.pdf"))
        # 目标被移走/删除
        os.unlink(a_op["dest"])
        # 原位置放入不同文件
        intruder = self.sandbox.downloads / "a.pdf"
        intruder.write_bytes(b"DIFFERENT-LENGTH-CONTENT")
        before = intruder.read_bytes()

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)

        entry = next(e for e in undo.entries if e["op_id"] == a_op["op_id"])
        self.assertNotIn(entry["undo_state"], {"restored"}, "绝不误报 restored")
        # 目标已被移走 → local 跳过（源位置又有不同文件，绝不覆盖）
        self.assertEqual(entry["undo_state"], "blocked_dest_missing")
        self.assertEqual(intruder.read_bytes(), before)

    def test_s16_variant_same_size_different_mtime_is_not_restored(self) -> None:
        plan, _ = self._execute_two()
        a_op = next(op for op in plan["operations"] if op["source"].endswith("a.pdf"))
        os.unlink(a_op["dest"])
        intruder = self.sandbox.downloads / "a.pdf"
        intruder.write_bytes(b"CONTENT-A")  # 同名同大小，但不同 inode
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        entry = next(e for e in undo.entries if e["op_id"] == a_op["op_id"])
        self.assertNotEqual(entry["undo_state"], "restored")
        self.assertEqual(entry["undo_state"], "blocked_dest_missing")

    def test_s16_variant_dest_replaced_is_blocked_dest_changed(self) -> None:
        plan, _ = self._execute_two()
        a_op = next(op for op in plan["operations"] if op["source"].endswith("a.pdf"))
        Path(a_op["dest"]).write_bytes(b"REPLACED-BY-USER")
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        entry = next(e for e in undo.entries if e["op_id"] == a_op["op_id"])
        self.assertEqual(entry["undo_state"], "blocked_dest_changed")

    def test_undo_state_is_mutually_exclusive(self) -> None:
        valid = {
            "pending", "attempting", "restored", "blocked_source_occupied",
            "blocked_dest_changed", "blocked_dest_missing", "blocked_dir_changed",
            "skipped_not_moved", "unknown_needs_review",
        }
        plan, _ = self._execute_two()
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        seen = set()
        for entry in undo.entries:
            self.assertIn(entry["undo_state"], valid)
            self.assertNotIn(entry["op_id"], seen, "每个 op_id 只能出现一次")
            seen.add(entry["op_id"])
        snapshot = read_snapshot(self.sandbox.settings, plan["plan_id"])
        for entry in snapshot["entries"]:
            self.assertIsInstance(entry["undo_state"], str)
            self.assertNotIn("/", entry["undo_state"])


class DecisionTableTests(SandboxTestCase):
    """收口断言：撤销恢复状态表覆盖全部组合且唯一决策。"""

    def _identity(self, **overrides) -> dict:
        base = {
            "st_dev": 1, "st_ino": 100, "size_bytes": 10, "mtime_ns": 500, "mode_type": "regular",
        }
        base.update(overrides)
        return base

    def test_undo_recovery_state_table_complete_and_unique(self) -> None:
        from doc_selecter.atomic_move import FileIdentity

        expected = self._identity()
        match = FileIdentity(1, 100, 10, 500, "regular")
        mismatch = FileIdentity(1, 999, 10, 500, "regular")

        # 源 3 态（匹配/不匹配/不存在）× 目标 3 态（匹配/不匹配/不存在）= 9 种组合，
        # 每种组合只能有一个决策（互斥）。
        cases = [
            (match, None, "restored"),
            (match, match, "unknown_needs_review"),      # 同一 inode 两处存在
            (match, mismatch, "blocked_dest_changed"),   # 目标被替换
            (mismatch, match, "blocked_source_occupied"),
            (mismatch, mismatch, "blocked_dest_changed"),
            (mismatch, None, "blocked_dest_missing"),
            (None, match, "attempting"),
            (None, mismatch, "blocked_dest_changed"),
            (None, None, "unknown_needs_review"),
        ]
        seen_states = set()
        for source, dest, want in cases:
            state, code, reason, scope = decide_attempting(
                source_identity=source, dest_identity=dest, expected=expected
            )
            self.assertEqual(state, want, f"S={source} D={dest}")
            self.assertIn(scope, {"none", "local", "global"})
            self.assertTrue(reason)
            self.assertTrue(code)
            seen_states.add(state)
        # 覆盖「不存在/匹配/不匹配」两侧的全部 9 种组合，且每种组合只有一个决策
        self.assertEqual(len(cases), 9)
        self.assertEqual(len({(str(a), str(b)) for a, b, _ in cases}), 9, "组合不得重复")
        self.assertEqual(
            seen_states,
            {
                "restored", "attempting", "blocked_source_occupied",
                "blocked_dest_changed", "blocked_dest_missing", "unknown_needs_review",
            },
        )

    def test_decide_rejects_identity_with_wrong_type(self) -> None:
        from doc_selecter.atomic_move import FileIdentity

        expected = self._identity()
        dir_identity = FileIdentity(1, 100, 10, 500, "dir")
        state, code, reason, scope = decide_attempting(
            source_identity=dir_identity, dest_identity=None, expected=expected
        )
        self.assertNotEqual(state, "restored", "目录绝不能算作已还原")


class RebuildIdentityTests(SandboxTestCase):
    """S17 + 收口断言：重建不得重设可信基准，也不得写 undo_intent。"""

    def test_s17_rebuild_uses_persisted_identity_not_current_observation(self) -> None:
        self.sandbox.add_file("a.pdf", b"ORIGINAL-PAYLOAD")
        plan = self.sandbox.make_plan()
        op = plan["operations"][0]
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])

        snapshot = read_snapshot(self.sandbox.settings, plan["plan_id"])
        persisted = dict(snapshot["entries"][0]["dest_identity"])

        # 用户改动了目标文件（大小与 mtime 都变）
        Path(op["dest"]).write_bytes(b"USER-EDITED-MUCH-LONGER-CONTENT")
        current = lstat_identity(op["dest"]).as_dict()
        self.assertNotEqual(
            (current["size_bytes"], current["mtime_ns"]),
            (persisted["size_bytes"], persisted["mtime_ns"]),
        )

        # 删除快照，逼出「从执行日志重建」
        os.unlink(snapshot_path(self.sandbox.settings, plan["plan_id"]))
        undo_journal = self.sandbox.journal(plan["plan_id"], "undo")
        events_before = undo_journal.read_events()[0]
        self.assertEqual(events_before, [], "重建前不应有任何撤销事件")

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)

        entry = undo.entries[0]
        self.assertEqual(entry["dest_identity"]["size_bytes"], persisted["size_bytes"],
                         "重建后的基准必须来自持久化记录，而不是当前观测值")
        self.assertEqual(entry["dest_identity"]["st_ino"], persisted["st_ino"])
        self.assertEqual(entry["undo_state"], "blocked_dest_changed", "已改动的文件必须拒绝撤销")
        self.assertEqual(Path(op["dest"]).read_bytes(), b"USER-EDITED-MUCH-LONGER-CONTENT",
                         "绝不能覆盖用户改动后的文件")

        # 重建过程不得写 undo_intent
        events_after = undo_journal.read_events()[0]
        kinds = [e["event"] for e in events_after]
        self.assertNotIn("undo_intent", kinds, "拒绝撤销时不得留下孤立 undo_intent")

    def test_rebuild_does_not_write_undo_intent(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        os.unlink(snapshot_path(self.sandbox.settings, plan["plan_id"]))

        # 只重建快照（不执行撤销）
        snapshot, _ = undo_mod.load_or_rebuild_snapshot(
            self.sandbox.settings, plan["plan_id"], write=True
        )
        self.assertTrue(snapshot["entries"])
        journal = self.sandbox.journal(plan["plan_id"], "undo")
        self.assertEqual(journal.read_events()[0], [], "重建快照不得写任何撤销事件")
        # 重建后条目仍是 pending，等待用户确认
        self.assertEqual(snapshot["entries"][0]["undo_state"], "pending")

    def test_rebuild_preserves_prior_undo_events(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD-A")
        self.sandbox.add_file("b.pdf", b"PAYLOAD-B")
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.restored, 2)

        # 删除快照后重建：已还原状态必须保留，不能一律重置为 pending
        os.unlink(snapshot_path(self.sandbox.settings, plan["plan_id"]))
        rebuilt, _ = undo_mod.load_or_rebuild_snapshot(
            self.sandbox.settings, plan["plan_id"], write=True
        )
        states = [e["undo_state"] for e in rebuilt["entries"]]
        self.assertEqual(states, ["restored", "restored"], "已有撤销事件必须被保留")
        self.assertEqual(rebuilt["undo_status"], "completed")
        journal = self.sandbox.journal(plan["plan_id"], "undo")
        kinds = [e["event"] for e in journal.read_events()[0]]
        self.assertEqual(kinds.count("undo_intent"), 2, "重建不得追加新的 undo_intent")


class UndoInterruptionTests(SandboxTestCase):
    """S13 + 收口断言：只有 attempting 进入中断判定，续作复用 intent。"""

    def _execute(self, names=("a.pdf", "b.pdf", "c.pdf")) -> dict:
        for name in names:
            self.sandbox.add_file(name, b"PAYLOAD-" + name.encode())
        plan = self.sandbox.make_plan()
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, len(names), result.outcomes)
        return plan

    def test_s13_only_attempting_enters_recovery_and_pending_is_not_global(self) -> None:
        plan = self._execute(("a.pdf", "b.pdf"))
        journal = self.sandbox.journal(plan["plan_id"], "undo")
        ops = {op["source"].rsplit("/", 1)[-1]: op for op in plan["operations"]}

        # 人为制造：a.pdf 的还原已经完成（原位置存在、目标不存在），intent 已写、result 未写
        a_op = ops["a.pdf"]
        os.replace(a_op["dest"], a_op["source"])
        journal.append(
            make_event("undo_intent", a_op["op_id"],
                       **{"from": a_op["dest"], "to": a_op["source"],
                          "expected_identity": a_op["identity"]})
        )
        # b.pdf 仍是普通 pending（尚无任何撤销事件）
        snapshot = read_snapshot(self.sandbox.settings, plan["plan_id"])
        states = {e["op_id"]: e["undo_state"] for e in snapshot["entries"]}
        self.assertEqual(states[b_op_id := ops["b.pdf"]["op_id"]], "pending")

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        final = {e["op_id"]: e["undo_state"] for e in undo.entries}

        self.assertEqual(final[a_op["op_id"]], "restored", "已确定还原的 attempting 项应补记 restored")
        self.assertEqual(final[b_op_id], "restored", "普通 pending 不得被判为全局未知")
        self.assertNotEqual(undo.undo_status, "needs_review")
        events = [e["event"] for e in journal.read_events()[0]]
        self.assertEqual(events.count("undo_intent"), 2, "a.pdf 不得被追加第二条 undo_intent")

    def test_resume_undo_reuses_intent_on_repeated_interruptions(self) -> None:
        plan = self._execute(("a.pdf",))
        op = plan["operations"][0]
        journal = self.sandbox.journal(plan["plan_id"], "undo")

        # 制造「intent 已写、尚未移动、未写 result」的中断现场（源不存在、目标匹配）
        journal.append(
            make_event("undo_intent", op["op_id"],
                       **{"from": op["dest"], "to": op["source"],
                          "expected_identity": op["identity"]})
        )
        for _ in range(3):
            # 反复中断续作：每次都因 resume=False 而不移动，intent 必须保持唯一
            result = undo_mod.undo_plan(
                self.sandbox.settings, plan["plan_id"], confirm=True, resume=False
            )
            self.assertEqual(result.restored, 0)
            events = [e["event"] for e in journal.read_events()[0]]
            self.assertEqual(events.count("undo_intent"), 1, "续作不得重复追加 undo_intent")

        # 最终续作成功：一条 intent + 一条 result
        final = undo_mod.undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True, resume=True)
        self.assertEqual(final.restored, 1, final.entries)
        events = [e["event"] for e in journal.read_events()[0]]
        self.assertEqual(events.count("undo_intent"), 1)
        self.assertEqual(events.count("undo_result"), 1)

    def test_undo_precheck_skip_is_durable_and_has_no_orphan_result(self) -> None:
        plan = self._execute(("a.pdf",))
        op = plan["operations"][0]
        # 原位置被占用 → 预检即应阻止
        intruder = self.sandbox.downloads / "a.pdf"
        intruder.write_bytes(b"USER")

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        self.assertEqual(undo.entries[0]["undo_state"], "blocked_source_occupied")

        journal = self.sandbox.journal(plan["plan_id"], "undo")
        events = journal.read_events()[0]
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds, ["undo_skip"], "预检跳过应写 undo_skip，不产生孤立 result")
        skip = events[0]
        self.assertEqual(skip["undo_state"], "blocked_source_occupied")
        self.assertIn("expected_identity", skip)
        self.assertEqual(intruder.read_bytes(), b"USER")

        # 再次撤销：不得重复写 undo_skip
        undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        kinds2 = [e["event"] for e in journal.read_events()[0]]
        self.assertEqual(kinds2.count("undo_skip"), 1)


class LockingTests(SandboxTestCase):
    """S14：进程互斥、重复执行防护、未解析 intent 准入。"""

    def test_s14_concurrent_execute_second_process_rejected(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        lock = GlobalLock(self.sandbox.settings)
        handle = lock.acquire(kind="cli", action="execute", plan_id=plan["plan_id"])
        try:
            with self.assertRaises(LockBusy) as ctx:
                lock2 = GlobalLock(self.sandbox.settings)
                lock2.acquire(kind="cli", action="execute", plan_id=plan["plan_id"])
            self.assertIn("另一个实例", str(ctx.exception))
        finally:
            lock.release(handle)

    def test_s14_two_processes_are_mutually_exclusive(self) -> None:
        """真实子进程持锁时，第二个进程必须被拒绝（跨进程 flock）。"""
        import subprocess
        import sys

        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        project_root = str(Path(__file__).resolve().parents[1])
        script = (
            "import os,sys,time\n"
            "sys.path.insert(0, %r)\n"
            "from doc_selecter.config import load_settings\n"
            "from doc_selecter.locking import GlobalLock\n"
            "s = load_settings()\n"
            "GlobalLock(s).acquire(kind='cli', action='execute', plan_id='probe')\n"
            "open(%r, 'w').write(s.lock_path)\n"
            "time.sleep(30)\n"
        ) % (project_root, str(self.sandbox.root / "child-locked"))
        errlog = self.sandbox.root / "child.err"
        with open(errlog, "wb") as errfh:
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.DEVNULL, stderr=errfh, env={**os.environ},
            )
        try:
            sentinel = self.sandbox.root / "child-locked"
            deadline = time.time() + 20
            while not sentinel.exists() and time.time() < deadline and proc.poll() is None:
                time.sleep(0.05)
            if not sentinel.exists():
                proc.wait(timeout=5)
                self.fail(
                    f"子进程未能持锁（returncode={proc.returncode}）："
                    f"{errlog.read_text(encoding='utf-8', errors='replace')}"
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"),
                             self.sandbox.settings.lock_path, "父子必须锁定同一个文件")

            lock = GlobalLock(self.sandbox.settings)
            with self.assertRaises(LockBusy):
                lock.acquire(kind="cli", action="execute", plan_id="probe")
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_s14_lock_released_on_process_death(self) -> None:
        import subprocess
        import sys
        import time

        script = (
            "import sys,time\n"
            "sys.path.insert(0, %r)\n"
            "from doc_selecter.config import load_settings\n"
            "from doc_selecter.locking import GlobalLock\n"
            "s = load_settings()\n"
            "GlobalLock(s).acquire(kind='cli', action='execute', plan_id='probe')\n"
            "print('LOCKED', flush=True)\n"
            "time.sleep(30)\n"
        ) % str(Path(__file__).resolve().parents[1])
        sentinel = self.sandbox.root / "death-locked"
        script2 = script.replace("print('LOCKED', flush=True)",
                                 f"open({str(sentinel)!r}, 'w').write('locked')")
        with open(self.sandbox.root / "death.err", "wb") as errfh:
            proc = subprocess.Popen(
                [sys.executable, "-c", script2],
                stdout=subprocess.DEVNULL, stderr=errfh, env={**os.environ},
            )
        try:
            deadline = time.time() + 20
            while not sentinel.exists() and time.time() < deadline and proc.poll() is None:
                time.sleep(0.05)
            self.assertTrue(sentinel.exists(), "子进程未能持锁")
            proc.kill()
            proc.wait(timeout=5)
            time.sleep(0.2)
            lock = GlobalLock(self.sandbox.settings)
            handle = lock.acquire(kind="cli", action="execute", plan_id="probe")
            lock.release(handle)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_second_execute_after_completed_rejected(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(current_phase(self.sandbox.settings, plan["plan_id"]), "executed")
        from doc_selecter.executor import ExecutionRefused

        with self.assertRaises(ExecutionRefused):
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])

    def test_s14_execute_rejected_while_intent_unresolved(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        from doc_selecter.directory_archive import ensure_layout
        ensure_layout(self.sandbox.settings,plan)
        journal = self.sandbox.journal(plan["plan_id"])
        op = plan["operations"][0]
        journal.append(
            make_event("intent", op["op_id"], source=op["source"], dest=op["dest"],
                       category=op["category"], identity=op["identity"])
        )
        from doc_selecter.executor import ExecutionRefused

        with self.assertRaises(ExecutionRefused) as ctx:
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertIn("recover", str(ctx.exception))
        self.assertExists(self.sandbox.downloads / "a.pdf")

    def test_plan_state_atomic_replace(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        state = read_plan_state(self.sandbox.settings, plan["plan_id"])
        self.assertEqual(state["phase"], "executed")
        leftovers = [p for p in self.sandbox.state.rglob("*.tmp-*")]
        self.assertEqual(leftovers, [], "原子替换不得留下临时文件")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
