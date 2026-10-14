"""契约与基建断言（docs/MVP方案.md §8.2）。

覆盖：plan_hash 契约、预览只读、敏感名称不落盘、日志持久化顺序、
幂等与互斥、原子原语、撤销范围语义、以及 v3.1 收口补充断言。
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from tests.harness import SandboxTestCase, snapshot_dir

from doc_selecter import journal as journal_mod
from doc_selecter import planner as planner_mod
from doc_selecter.atomic_move import AtomicMoveUnavailable, lstat_identity
from doc_selecter.config import ConfigError, TEST_ROOT_ENV, load_settings
from doc_selecter.executor import ExecutionRefused, execute_plan, probe_capability
from doc_selecter.journal import Journal, JournalCorrupted, make_event
from doc_selecter.plan_hash import (
    canonical_json,
    compute_plan_hash,
    normalize_path,
    plan_body_digest,
    verify_plan_hash,
)
from doc_selecter.planner import build_plan, write_confirmation, write_plan
from doc_selecter.rules import CATEGORIES
from doc_selecter.snapshot import read_snapshot
from doc_selecter.undo import undo_plan


class PlanHashContractTests(SandboxTestCase):
    """test_plan_hash_contract：覆盖字段敏感、排除字段不敏感、计算可复现。"""

    def _plan(self) -> dict:
        for name in ("a.pdf", "b.png"):
            self.sandbox.add_file(name, b"PAYLOAD")
        return self.sandbox.make_plan(confirm=False)

    def test_valid_fields_change_hash(self) -> None:
        plan = self._plan()
        base = plan["plan_hash"]
        mutations = {
            "改哈希版本": lambda p: p.update({"plan_hash_alg": "other"}),
            "增删 operation": lambda p: p["operations"].pop(),
            "改 dest": lambda p: p["operations"][0].__setitem__("dest", p["operations"][0]["dest"] + ".renamed"),
            "改 source": lambda p: p["operations"][0].__setitem__("source", p["operations"][0]["source"] + "x"),
            "改勾选集合": lambda p: p.update({"category_selection": ["文档资料"]}),
            "改 identity.size": lambda p: p["operations"][0]["identity"].__setitem__(
                "size_bytes", p["operations"][0]["identity"]["size_bytes"] + 1),
            "改 identity.st_ino": lambda p: p["operations"][0]["identity"].__setitem__(
                "st_ino", p["operations"][0]["identity"]["st_ino"] + 1),
            "改 identity.mtime": lambda p: p["operations"][0]["identity"].__setitem__(
                "mtime_ns", p["operations"][0]["identity"]["mtime_ns"] + 1),
            "改 identity.type": lambda p: p["operations"][0]["identity"].__setitem__(
                "mode_type", "other"),
            "改规则版本": lambda p: p.update({"rules_version": "other-rules"}),
            "改目标根": lambda p: p.update({"target_root": p["target_root"] + "-2"}),
            "改下载目录": lambda p: p.update({"downloads_dir": p["downloads_dir"] + "-2"}),
            "改 schema": lambda p: p.update({"schema_version": 99}),
            "改分类依据": lambda p: p["operations"][0].__setitem__("classification_basis", "x"),
            "改 classify 类别": lambda p: p["operations"][0].__setitem__("category", "音视频"),
        }
        for label, mutate in mutations.items():
            copy = json.loads(json.dumps(plan))
            mutate(copy)
            changed = compute_plan_hash(copy)
            self.assertNotEqual(changed, base, f"有效字段改动必须改变 plan_hash：{label}")

    def test_excluded_fields_do_not_change_hash(self) -> None:
        plan = self._plan()
        base = plan["plan_hash"]
        excluded = {
            "plan_id": lambda p: p.update({"plan_id": "plan-19990101-000000-abcdef"}),
            "created_at": lambda p: p.update({"created_at": "1999-01-01T00:00:00+0000"}),
            "counts": lambda p: p.update({"counts": {"included": 999}}),
            "excluded": lambda p: p.update({"excluded": {"文件夹": ["x"]}}),
            "category_totals": lambda p: p.update({"category_totals": {}}),
            "plan_hash 自身": lambda p: p.update({"plan_hash": "sha256:deadbeef"}),
            "运行期字段": lambda p: p.update({"journal_offset": 123, "phase": "executing"}),
        }
        for label, mutate in excluded.items():
            copy = json.loads(json.dumps(plan))
            mutate(copy)
            self.assertEqual(
                compute_plan_hash(copy), base, f"排除字段不得改变 plan_hash：{label}"
            )

    def test_hash_is_reproducible_and_nfc_normalized(self) -> None:
        plan = self._plan()
        self.assertEqual(compute_plan_hash(plan), compute_plan_hash(json.loads(json.dumps(plan))))
        self.assertTrue(verify_plan_hash(plan))

        # NFC / NFD 归一化：同一文件名的两种码点编码必须得到同一指纹
        nfc = "café.pdf"
        nfd = "cafe\u0301.pdf"
        self.assertNotEqual(nfc, nfd)
        self.assertEqual(canonical_json({"p": nfc}), canonical_json({"p": nfd}))

    def test_no_float_and_absolute_paths_required(self) -> None:
        plan = self._plan()
        bad = json.loads(json.dumps(plan))
        bad["operations"][0]["identity"]["size_bytes"] = 1.5
        with self.assertRaises(Exception):
            compute_plan_hash(bad)
        bad2 = json.loads(json.dumps(plan))
        bad2["downloads_dir"] = "relative/path"
        with self.assertRaises(Exception):
            compute_plan_hash(bad2)

    def test_operation_order_does_not_change_hash(self) -> None:
        plan = self._plan()
        base = compute_plan_hash(plan)
        shuffled = json.loads(json.dumps(plan))
        shuffled["operations"] = list(reversed(shuffled["operations"]))
        self.assertEqual(compute_plan_hash(shuffled), base, "operations 按 op_id 排序，顺序不敏感")

    def test_category_selection_order_normalized(self) -> None:
        plan = self._plan()
        copy = json.loads(json.dumps(plan))
        copy["category_selection"] = list(reversed(copy["category_selection"]))
        self.assertEqual(compute_plan_hash(copy), plan["plan_hash"], "固定类别顺序")

    def test_rebuild_preserves_prior_undo_events_across_recover(self) -> None:
        """收口断言：重建/恢复流程不得重置已有撤销状态。"""
        for name in ("a.pdf", "b.pdf"):
            self.sandbox.add_file(name, b"PAYLOAD-" + name.encode())
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])

        # 完成一次撤销（全部 restored），然后删除快照
        undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        from doc_selecter.snapshot import snapshot_path

        os.unlink(snapshot_path(self.sandbox.settings, plan["plan_id"]))

        # recover 重建快照：必须保留 restored，不得重置为 pending
        from doc_selecter.executor import recover_execution

        report = recover_execution(self.sandbox.settings, plan["plan_id"])
        snapshot = read_snapshot(self.sandbox.settings, plan["plan_id"])
        states = [e["undo_state"] for e in snapshot["entries"]]
        self.assertEqual(states, ["restored", "restored"], report)
        self.assertEqual(snapshot["undo_status"], "completed")
        # 恢复流程自身不得写入 undo_intent
        journal = self.sandbox.journal(plan["plan_id"], "undo")
        kinds = [e["event"] for e in journal.read_events()[0]]
        self.assertEqual(kinds.count("undo_intent"), 2, "恢复不得追加新的 undo_intent")

    def test_changed_selection_changes_hash_and_confirm(self) -> None:
        plan = self._plan()
        path = self.sandbox.plan_file(plan["plan_id"])
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["category_selection"] = ["文档资料"]
        tampered["operations"] = [
            op for op in tampered["operations"] if op["category"] == "文档资料"
        ]
        path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(verify_plan_hash(tampered), "被改动的计划指纹必须失配")


class ReadOnlyPreviewTests(SandboxTestCase):
    def test_planning_does_not_write_inside_downloads(self) -> None:
        for name, payload in (("a.pdf", b"A"), ("b.zip", b"B"), ("c.png", b"C")):
            self.sandbox.add_file(name, payload)
        self.sandbox.add_dir("folder")
        self.sandbox.add_symlink("link.pdf", "a.pdf")

        before = snapshot_dir(self.sandbox.downloads)
        before_dirs = sorted(p.name for p in self.sandbox.downloads.iterdir())
        plan = self.sandbox.make_plan(confirm=False)
        after = snapshot_dir(self.sandbox.downloads)
        after_dirs = sorted(p.name for p in self.sandbox.downloads.iterdir())

        self.assertEqual(before, after, "预览不得改动下载目录内任何条目")
        self.assertEqual(before_dirs, after_dirs, "预览不得在下载目录内新建条目")
        self.assertTrue((self.sandbox.state / "plans" / f"{plan['plan_id']}.json").exists(),
                        "写入只应发生在状态目录")

    def test_capability_probe_does_not_touch_downloads(self) -> None:
        self.sandbox.add_file("a.pdf", b"A")
        before = snapshot_dir(self.sandbox.downloads)
        probe_capability(self.sandbox.settings)
        self.assertEqual(snapshot_dir(self.sandbox.downloads), before)
        junk = [p.name for p in self.sandbox.state.iterdir() if p.name.startswith(".probe")]
        self.assertEqual(junk, [], "探测文件必须清理干净")


class SensitivePrivacyTests(SandboxTestCase):
    def test_sensitive_name_never_persisted(self) -> None:
        secret = "id_rsa"
        self.sandbox.add_file(secret, b"-----BEGIN OPENSSH PRIVATE KEY-----")
        self.sandbox.add_file("api_token_backup.txt", b"token=abcdef")
        self.sandbox.add_file("normal.pdf", b"FINE")
        plan = self.sandbox.make_plan()
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)

        blob = ""
        for path in sorted(self.sandbox.state.rglob("*")):
            if path.is_file():
                blob += path.read_text(encoding="utf-8", errors="replace")
        for name in (secret, "api_token_backup.txt"):
            self.assertNotIn(name, blob, f"疑似敏感项名称不得落盘：{name}")
        # 只保留数量
        self.assertEqual(plan["excluded"]["疑似敏感项"], {"count": 2, "names": None})
        self.assertExists(self.sandbox.downloads / secret)

    def test_secret_file_untouched_by_execution(self) -> None:
        secret = self.sandbox.add_file("password.txt", b"hunter2")
        before = (secret.read_bytes(), os.lstat(secret).st_mtime_ns, os.lstat(secret).st_ino)
        self.sandbox.add_file("a.pdf", b"A")
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        after = (secret.read_bytes(), os.lstat(secret).st_mtime_ns, os.lstat(secret).st_ino)
        self.assertEqual(before, after, "疑似敏感文件必须完全未被触碰")


class JournalDurabilityTests(SandboxTestCase):
    """test_intent_fsynced_before_move / test_no_buffered_batch_write。"""

    def test_intent_fsynced_before_move(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        order: list[str] = []

        real_append = Journal.append
        real_write = os.write
        real_fsync = os.fsync

        def spy_append(self, payload, *, report=None):  # noqa: ANN001
            order.append(f"append:{payload.get('event')}")
            return real_append(self, payload, report=report)

        def spy_write(fd, data):
            if isinstance(data, (bytes, bytearray)) and b'"event"' in data and b'"op_kind":"file"' in data:
                event = "intent" if b'"intent"' in data else (
                    "result" if b'"result"' in data else "other"
                )
                order.append(f"write:{event}")
            return real_write(fd, data)

        def spy_fsync(fd):
            order.append("fsync")
            return real_fsync(fd)

        real_mover = None
        from doc_selecter import executor as executor_mod

        real_mover = executor_mod.AtomicMover

        class Spy(real_mover):  # type: ignore[misc, valid-type]
            def rename_excl_at(self, sfd, sname, dfd, dname):  # type: ignore[override]
                order.append("rename")
                return super().rename_excl_at(sfd, sname, dfd, dname)

        Journal.append = spy_append  # type: ignore[assignment]
        os.write = spy_write  # type: ignore[assignment]
        os.fsync = spy_fsync  # type: ignore[assignment]
        executor_mod.AtomicMover = Spy  # type: ignore[misc]
        try:
            execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        finally:
            Journal.append = real_append  # type: ignore[assignment]
            os.write = real_write  # type: ignore[assignment]
            os.fsync = real_fsync  # type: ignore[assignment]
            executor_mod.AtomicMover = real_mover  # type: ignore[misc]

        # 关键顺序：intent 写入 → fsync → rename → result 写入 → fsync
        idx_intent_write = order.index("write:intent")
        idx_intent_fsync = order.index("fsync", idx_intent_write)
        idx_rename = order.index("rename")
        idx_result_write = order.index("write:result")
        idx_result_fsync = order.index("fsync", idx_result_write)
        self.assertLess(idx_intent_write, idx_intent_fsync)
        self.assertLess(idx_intent_fsync, idx_rename, "intent 必须先 fsync 再移动")
        self.assertLess(idx_rename, idx_result_write)
        self.assertLess(idx_result_write, idx_result_fsync)

    def test_no_buffered_batch_write(self) -> None:
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            self.sandbox.add_file(name, b"PAYLOAD")
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])

        journal = self.sandbox.journal(plan["plan_id"])
        # 每次 append 恰好写入一行且立即 fsync：行数 == 事件数
        events, _ = journal.read_events()
        manifest = json.loads(Path(journal.manifest_path).read_text(encoding="utf-8"))
        total_lines = 0
        for gen in manifest["generations"]:
            data = (Path(journal.dir) / gen["file"]).read_bytes()
            total_lines += data.count(b"\n")
        self.assertEqual(total_lines, len(events), "不得合并写入：行数必须等于事件数")
        self.assertEqual(len([e for e in events if e.get("op_kind") == "file"]), 6, "3 个文件 = 3 条 intent + 3 条 result")

    def test_every_intent_has_result(self) -> None:
        for name in ("a.pdf", "b.pdf"):
            self.sandbox.add_file(name, b"PAYLOAD")
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        fold, report = self.sandbox.journal(plan["plan_id"]).fold()
        self.assertIsNone(report.truncated_tail_line)
        for op_id, slot in fold.items():
            self.assertIsNotNone(slot["intent"], f"{op_id} 缺少 intent")
            self.assertIsNotNone(slot["result"], f"{op_id} 缺少 result")


class ScopeSemanticsTests(SandboxTestCase):
    """test_undo_local_conflict_continues / test_undo_global_issue_stops_batch。"""

    def _executed(self, names) -> dict:
        for name in names:
            self.sandbox.add_file(name, b"PAYLOAD-" + name.encode())
        plan = self.sandbox.make_plan()
        result = execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        self.assertEqual(result.moved, len(names), result.outcomes)
        return plan

    def test_undo_local_conflict_continues(self) -> None:
        plan = self._executed(("a.pdf", "b.pdf", "c.pdf"))
        intruder = self.sandbox.downloads / "b.pdf"
        intruder.write_bytes(b"USER-FILE")
        before = intruder.read_bytes()

        undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)

        states = {e["original_source"].rsplit("/", 1)[-1]: e["undo_state"] for e in undo.entries}
        self.assertEqual(states["b.pdf"], "blocked_source_occupied")
        self.assertEqual(states["a.pdf"], "restored", "局部冲突必须继续处理其他项")
        self.assertEqual(states["c.pdf"], "restored")
        self.assertEqual(undo.undo_status, "partial_undo")
        self.assertEqual(intruder.read_bytes(), before)

    def test_undo_global_issue_stops_batch(self) -> None:
        plan = self._executed(("a.pdf", "b.pdf", "c.pdf"))
        from doc_selecter import undo as undo_mod

        original = undo_mod.dir_identity
        calls = {"n": 0}

        def flaky(path: str):
            # 第一次调用（下载目录）返回被篡改的身份 → global 停止
            calls["n"] += 1
            if calls["n"] == 1:
                return (-1, -1)
            return original(path)

        undo_mod.dir_identity = flaky
        try:
            undo = undo_plan(self.sandbox.settings, plan["plan_id"], confirm=True)
        finally:
            undo_mod.dir_identity = original

        self.assertEqual(undo.restored, 0)
        self.assertTrue(undo.stopped, "目录身份变化必须停止整批")
        self.assertIn(undo.undo_status, {"needs_review", "aborted_global"})
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            self.assertExists(self.sandbox.downloads / "下载分类" / "文档资料" / "PDF" / name)


class AtomicPrimitiveTests(SandboxTestCase):
    def test_atomic_move_unavailable_blocks_execution_with_zero_moves(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()

        class Unavailable:
            available = False
            unavailable_reason = "打桩：原语不可用"

            def probe_capability(self, *a, **k):
                raise AtomicMoveUnavailable("打桩")

        from doc_selecter import executor as executor_mod

        original = executor_mod.AtomicMover
        executor_mod.AtomicMover = Unavailable  # type: ignore[assignment]
        try:
            with self.assertRaises(ExecutionRefused) as ctx:
                execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        finally:
            executor_mod.AtomicMover = original  # type: ignore[assignment]

        self.assertIn("原子不覆盖", str(ctx.exception))
        self.assertExists(self.sandbox.downloads / "a.pdf")
        self.assertNotExists(self.sandbox.downloads / "下载分类" / "文档资料" / "a.pdf")

    def test_capability_probe_failure_blocks_execution(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()

        from doc_selecter import executor as executor_mod

        original = executor_mod.AtomicMover

        class Broken(original):  # type: ignore[misc, valid-type]
            def probe_capability(self, *a, **k):
                raise AtomicMoveUnavailable("打桩：探测失败")

        executor_mod.AtomicMover = Broken  # type: ignore[assignment]
        try:
            with self.assertRaises(ExecutionRefused):
                execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        finally:
            executor_mod.AtomicMover = original  # type: ignore[assignment]
        self.assertExists(self.sandbox.downloads / "a.pdf")


class ConfigGateTests(SandboxTestCase):
    """缺少沙盒配置即拒绝运行，绝不回退到真实 Downloads。"""

    def test_missing_config_refuses(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_settings("/nonexistent/config.json", env={})
        self.assertIn("拒绝运行", str(ctx.exception))

    def test_env_gate_used_when_present(self) -> None:
        settings = load_settings(env={TEST_ROOT_ENV: str(self.sandbox.root)})
        self.assertTrue(settings.sandboxed)
        self.assertEqual(settings.downloads_dir, str(self.sandbox.downloads))
        self.assertEqual(settings.state_dir, str(self.sandbox.state))

    def test_state_dir_inside_downloads_refused(self) -> None:
        cfg = self.sandbox.root / "cfg.json"
        cfg.write_text(json.dumps({
            "downloads_dir": str(self.sandbox.downloads),
            "state_dir": str(self.sandbox.downloads / ".state"),
            "target_parent_name": "下载分类",
        }), encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_settings(str(cfg), env={})
        self.assertIn("状态目录位于下载目录内", str(ctx.exception))

    def test_project_dir_refused(self) -> None:
        from doc_selecter.config import PROJECT_DIR

        cfg = self.sandbox.root / "cfg2.json"
        cfg.write_text(json.dumps({
            "downloads_dir": str(self.sandbox.downloads),
            "state_dir": os.path.join(PROJECT_DIR, ".state"),
        }), encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_settings(str(cfg), env={})
        self.assertIn("实现目录", str(ctx.exception))

    def test_illegal_conflict_policy_refused(self) -> None:
        cfg = self.sandbox.root / "cfg3.json"
        cfg.write_text(json.dumps({
            "downloads_dir": str(self.sandbox.downloads),
            "state_dir": str(self.sandbox.state),
            "on_name_conflict": "overwrite",
        }), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_settings(str(cfg), env={})

    def test_rename_primitive_cannot_be_downgraded(self) -> None:
        cfg = self.sandbox.root / "cfg4.json"
        cfg.write_text(json.dumps({
            "downloads_dir": str(self.sandbox.downloads),
            "state_dir": str(self.sandbox.state),
            "move_primitive": "os.rename",
        }), encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_settings(str(cfg), env={})
        self.assertIn("rename_excl", str(ctx.exception))


class RuleTableTests(SandboxTestCase):
    """分类规则与基线一致（含固定裁定）。"""

    def test_csv_and_tsv_are_code_data(self) -> None:
        from doc_selecter.rules import classify_entry

        self.assertEqual(classify_entry("a.csv", 0o100644).category, "代码与数据")
        self.assertEqual(classify_entry("a.tsv", 0o100644).category, "代码与数据")

    def test_office_sheets_are_documents(self) -> None:
        from doc_selecter.rules import classify_entry

        for name in ("a.xlsx", "a.xls", "a.xlsm", "a.ods"):
            self.assertEqual(classify_entry(name, 0o100644).category, "文档资料", name)

    def test_ambiguous_extensions_are_unclassified(self) -> None:
        from doc_selecter.rules import classify_entry

        for name in ("a.ts", "a.jar"):
            self.assertEqual(classify_entry(name, 0o100644).category, "待确认", name)

    def test_longest_extension_wins(self) -> None:
        from doc_selecter.rules import classify_entry

        self.assertEqual(classify_entry("a.tar.gz", 0o100644).category, "压缩包")

    def test_every_file_gets_exactly_one_category(self) -> None:
        from doc_selecter.rules import CATEGORIES as CATS
        from doc_selecter.rules import classify_entry

        names = ["a.dmg", "b.pdf", "c.png", "d.mp4", "e.zip", "f.json", "g.unknown", "h"]
        for name in names:
            result = classify_entry(name, 0o100644)
            self.assertEqual(result.status, "included")
            self.assertIn(result.category, CATS)


class StatusAndCliTests(SandboxTestCase):
    """CLI 端到端（在沙盒内）。"""

    def _run(self, *argv: str) -> tuple[int, str]:
        import io
        import contextlib

        from doc_selecter.cli import main

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = main(list(argv))
        return code, buf.getvalue()

    def test_cli_full_loop(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        self.sandbox.add_file("b.zip", b"ZIP")

        code, out = self._run("plan")
        self.assertEqual(code, 0, out)
        self.assertIn("计划编号", out)
        plan_id = [line.split("：", 1)[1] for line in out.splitlines() if line.startswith("计划编号：")][0]
        plan_hash = [line.split("：", 1)[1] for line in out.splitlines() if line.startswith("计划指纹：")][0]
        self.assertIn("压缩包", out)

        code, out = self._run("confirm", "--plan", plan_id)
        self.assertEqual(code, 0, out)

        code, out = self._run("execute", "--plan", plan_id, "--confirm", plan_hash)
        self.assertEqual(code, 0, out)
        self.assertIn("成功 2", out)

        code, out = self._run("status", "--plan", plan_id)
        self.assertEqual(code, 0, out)
        self.assertIn("executed", out)

        code, out = self._run("undo", "--plan", plan_id, "--confirm")
        self.assertEqual(code, 0, out)
        self.assertExists(self.sandbox.downloads / "a.pdf")
        self.assertExists(self.sandbox.downloads / "b.zip")

    def test_cli_refuses_execute_without_confirm(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        code, out = self._run("plan")
        plan_id = [line.split("：", 1)[1] for line in out.splitlines() if line.startswith("计划编号：")][0]
        code, out = self._run("execute", "--plan", plan_id, "--confirm", "sha256:bad")
        self.assertNotEqual(code, 0)
        self.assertIn("拒绝执行", out)
        self.assertNotExists(self.sandbox.downloads / "下载分类")

    def test_cli_undo_requires_explicit_confirmation(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        plan = self.sandbox.make_plan()
        execute_plan(self.sandbox.settings, plan["plan_id"], plan["plan_hash"])
        code, out = self._run("undo", "--plan", plan["plan_id"])
        self.assertNotEqual(code, 0)
        self.assertIn("确认", out)
        self.assertExists(self.sandbox.downloads / "下载分类" / "文档资料" / "PDF" / "a.pdf")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
