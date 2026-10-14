"""撤销后的真实 Tk 状态、历史载入与旧计划回调准入。"""
from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import patch

from tests.harness import SandboxTestCase, require_gui_preview
from tests import test_gui as gui_tests
from doc_selecter import gui
from doc_selecter.atomic_move import AtomicMover, MoveFailed, lstat_identity
from doc_selecter.locking import LockBusy, current_phase, plan_state_path
from doc_selecter.planner import confirmation_path
from doc_selecter.snapshot import read_snapshot
from doc_selecter.undo import UndoRefused

RECORDS = []


class UndoGuiStateTests(SandboxTestCase):
    _build = gui_tests.GuiTests._build
    _stub_dialogs = gui_tests.GuiTests._stub_dialogs
    _restore_dialogs = gui_tests.GuiTests._restore_dialogs

    def setUp(self):
        super().setUp()
        self.assertTrue(gui_tests.DISPLAY_AVAILABLE, gui_tests._DISPLAY_ERROR)
        self.app = self.root = None
        self.before = {}
        for rel in ("Chosen/deep/说明.pdf", "Chosen/deep/图片.png", "ordinary.csv"):
            p = self.sandbox.add_file(rel, b"ORIGINAL-" + rel.encode())
            self.before[rel] = (lstat_identity(str(p)).as_dict(), p.read_bytes())
        self.neighbor = self.sandbox.add_file("Chosen/other/untouched.pdf", b"UNSELECTED")
        self.neighbor_identity = lstat_identity(str(self.neighbor)).as_dict()

    def tearDown(self):
        if self.app is not None:
            self.app.on_close()
        elif self.root is not None:
            self.root.destroy()
        super().tearDown()

    def preview(self):
        self.app = self._build()
        self.app.subfolder_var.set(True)
        self.app.on_toggle_subfolders()
        self.app.selected_scope = ["Chosen/deep"]
        self.app._on_scope_changed()
        require_gui_preview(self.app)
        self.assertEqual(len(self.app.plan["operations"]), 3)
        return self.app

    def execute(self):
        app = self.preview()
        app.on_confirm()
        app.on_execute()
        for op in app.plan["operations"]:
            self.assertIdentityMatches(op["dest"], op["identity"])
            self.assertNotExists(op["source"])
        self.assertEqual(current_phase(self.sandbox.settings, app.plan["plan_id"]), "executed")
        return app

    def reopen(self):
        self.app.on_close()
        self._restore_dialogs()
        self.app = self._build()
        self.assertEqual(self.app.selected_scope, [])
        self.assertFalse(self.app.subfolder_var.get())
        self.assertEqual(self.app.plan['selected_scope'], ['', 'Chosen/deep'])
        self.assertNotIn("Chosen/other", self.app.scope_summary())
        self.assertIsNone(self.app.confirmed_hash)
        return self.app

    def completed(self, count=3):
        self.assertIn(f"已撤销（已还原 {count} 项）", str(self.app.status_label["text"]))
        self.assertIn(f"撤销结果：已还原 {count}", str(self.app.result_label["text"]))
        self.assert_disabled()
        self.assertEqual(str(self.app.undo_button["state"]), "disabled")
        self.assertEqual(current_phase(self.sandbox.settings, self.app.plan["plan_id"]), "undone")
        self.assertEqual(read_snapshot(self.sandbox.settings, self.app.plan["plan_id"])["undo_status"], "completed")
        for rel, (identity, content) in self.before.items():
            p = self.sandbox.downloads / rel
            self.assertIdentityMatches(p, identity)
            self.assertEqual(p.read_bytes(), content)
            self.assertIn(p.name, os.listdir(p.parent))
        self.assertIdentityMatches(self.neighbor, self.neighbor_identity)
        self.assertEqual(self.neighbor.read_bytes(), b"UNSELECTED")
        for kind in ("execution", "undo"):
            self.assertEqual(self.sandbox.journal(self.app.plan["plan_id"], kind).classify().classification, "healthy")

    def assert_disabled(self):
        self.assertEqual(str(self.app.confirm_button["state"]), "disabled")
        self.assertEqual(str(self.app.execute_button["state"]), "disabled")

    def record(self, stage):
        app = self.app
        view = dict(stage=stage, sandbox=str(self.sandbox.root), plan_id=app.plan["plan_id"],
                    status=str(app.status_label["text"]), result=str(app.result_label["text"]),
                    buttons={name: str(getattr(app, name + "_button")["state"])
                             for name in ("confirm", "execute", "undo")},
                    selected_scope=app.selected_scope, recursive=app.subfolder_var.get(),
                    phase=current_phase(self.sandbox.settings, app.plan["plan_id"]),
                    snapshot=read_snapshot(self.sandbox.settings, app.plan["plan_id"]))
        RECORDS.append(view)
        return view

    def immutable(self):
        plan_id = self.app.plan["plan_id"]
        paths = [self.sandbox.plan_file(plan_id), Path(confirmation_path(self.sandbox.settings, plan_id))]
        for kind in ("execution", "undo"):
            path = Path(self.sandbox.journal(plan_id, kind).generation_file(1))
            if path.exists():
                paths.append(path)
        return {p: p.read_bytes() for p in paths}

    def reject_reuse(self):
        app, before = self.app, self.immutable()
        with patch.object(gui, "write_confirmation", wraps=gui.write_confirmation) as confirm, \
                patch.object(gui, "execute_plan", wraps=gui.execute_plan) as execute:
            app.on_confirm()
            app._preview_ready = True
            app.confirmed_hash = app.plan["plan_hash"]
            self.assertIsNone(app.perform_execution())
            app._preview_ready = True
            app.confirmed_hash = app.plan["plan_hash"]
            app.on_execute()
            self.assertFalse(confirm.called)
            self.assertFalse(execute.called)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assert_disabled()

    def block_sources(self, count):
        for op in self.app.plan["operations"][:count]:
            Path(op["source"]).write_bytes(b"ARTIFICIAL-OCCUPANT")

    def cleanup_partial(self):
        # 仅收回本测试的对抗材料；不改旧撤销记录或身份基准，不算产品撤销成功。
        for op in self.app.plan["operations"]:
            if Path(op["dest"]).is_file():
                source = Path(op["source"])
                if source.exists():
                    self.assertEqual(source.read_bytes(), b"ARTIFICIAL-OCCUPANT")
                    source.unlink()
                AtomicMover().rename_excl(op["dest"], op["source"])
            self.assertIdentityMatches(op["source"], op["identity"])
            self.assertEqual(Path(op["source"]).read_bytes(), self.before[op["rel_path"]][1])

    def test_complete_undo_displays_restored_count_and_blocks_old_callbacks(self):
        app = self.execute()
        app.on_undo()
        self.completed()
        self.assertIn("上次执行：成功 3", str(app.result_label["text"]))
        self.reject_reuse()
        self.record("completed")

    def test_execution_cancel_keeps_confirmed_preview_and_zero_journal(self):
        app = self.preview()
        app.on_confirm()
        before = self.record("before-execute-cancel")
        with patch.object(gui.messagebox, "askokcancel", return_value=False):
            app.on_execute()
        after = self.record("after-execute-cancel")
        self.assertEqual(before, dict(after, stage=before["stage"]))
        self.assertEqual(self.sandbox.journal(app.plan["plan_id"]).read_events()[0], [])
        for op in app.plan["operations"]:
            self.assertIdentityMatches(op["source"], op["identity"])

    def test_undo_cancel_keeps_execution_statistics_and_target_identities(self):
        app = self.execute()
        before, data = self.record("before-undo-cancel"), self.immutable()
        with patch.object(gui.messagebox, "askokcancel", return_value=False):
            app.on_undo()
        after = self.record("after-undo-cancel")
        self.assertEqual(before, dict(after, stage=before["stage"]))
        for p, content in data.items():
            self.assertEqual(p.read_bytes(), content)
        for op in app.plan["operations"]:
            self.assertIdentityMatches(op["dest"], op["identity"])
        app.on_undo()
        self.completed()

    def test_partial_undo_does_not_claim_all_restored(self):
        app = self.execute()
        self.block_sources(1)
        app.on_undo()
        self.assertIn("部分撤销", str(app.status_label["text"]))
        self.assertNotIn("已撤销", str(app.status_label["text"]))
        self.assertIn("已还原 2 · 被阻止 1", str(app.result_label["text"]))
        self.assertIn("上次执行：成功 3", str(app.result_label["text"]))
        self.assert_disabled()
        self.record("partial")
        self.cleanup_partial()

    def test_all_blocked_undo_displays_zero_restored(self):
        app = self.execute()
        self.block_sources(3)
        app.on_undo()
        self.assertIn("撤销未完成", str(app.status_label["text"]))
        self.assertIn("已还原 0 · 被阻止 3", str(app.result_label["text"]))
        self.assertNotIn("已撤销", str(app.status_label["text"]))
        self.record("all-blocked")
        self.cleanup_partial()

    def test_undo_exception_reports_failure_and_retry_works(self):
        app = self.execute()
        with patch.object(gui, "undo_plan", side_effect=UndoRefused("ARTIFICIAL refusal")):
            app.on_undo()
        self.assertIn("本次撤销失败", str(app.status_label["text"]))
        self.assertIn("本次撤销失败", str(app.result_label["text"]))
        self.assertNotIn("已撤销", str(app.status_label["text"]))
        self.assert_disabled()
        self.record("undo-request-failed")
        app.on_undo()
        self.completed()

    def test_directory_refusal_is_reported_without_losing_execution_counts(self):
        app = self.execute()
        parent = self.sandbox.downloads / "Chosen/deep"
        saved = self.sandbox.root / "saved-parent"
        parent.rename(saved)
        parent.mkdir()
        try:
            app.on_undo()
            self.assertIn("本次撤销失败", str(app.status_label["text"]))
            self.assertIn("上次执行：成功 3", str(app.result_label["text"]))
            self.assert_disabled()
            for op in app.plan["operations"]:
                self.assertIdentityMatches(op["dest"], op["identity"])
        finally:
            parent.rmdir()
            saved.rename(parent)
        app.on_undo()
        self.completed()

    def test_persisted_global_undo_failure_displays_needs_review(self):
        app = self.execute()
        with patch.object(AtomicMover, "rename_excl_at", side_effect=MoveFailed(errno.EACCES, "ARTIFICIAL permission failure")):
            app.on_undo()
        self.assertIn("需检查", str(app.status_label["text"]))
        self.assertNotIn("已撤销", str(app.status_label["text"]))
        self.assertIn("已还原 0", str(app.result_label["text"]))
        self.assert_disabled()
        self.assertEqual(str(app.undo_button["state"]), "disabled")
        self.record("global-failure")
        app.on_recover()
        self.assertIn("需检查", str(app.status_label["text"]))
        self.reopen()
        self.assertIn("需检查", str(self.app.status_label["text"]))
        self.record("reopened-global-failure")
        self.cleanup_partial()

    def test_recover_after_complete_undo_corrects_stale_widgets(self):
        app = self.execute()
        app.on_undo()
        app._set_status(gui.STATE_EXECUTED)
        app.result_label.configure(text="ARTIFICIAL STALE VIEW")
        app.on_recover()
        self.completed()
        self.record("recover-completed")

    def test_recover_after_partial_undo_preserves_partial_counts(self):
        app = self.execute()
        self.block_sources(1)
        app.on_undo()
        app.on_recover()
        self.assertIn("部分撤销", str(app.status_label["text"]))
        self.assertIn("已还原 2 · 被阻止 1", str(app.result_label["text"]))
        self.record("recover-partial")
        self.cleanup_partial()

    def test_recover_failure_is_explicit_and_old_execution_remains_disabled(self):
        app = self.execute()
        with patch.object(gui, "recover_execution", side_effect=LockBusy("ARTIFICIAL lock busy")):
            app.on_recover()
        self.assertIn("恢复/检查失败", str(app.status_label["text"]))
        self.assert_disabled()
        self.assertIn("成功 3", str(app.result_label["text"]))
        app.on_undo()
        self.completed()

    def test_reopen_executed_history_preserves_scope_counts_and_blocks_reuse(self):
        self.execute()
        self.reopen()
        self.assertIn("已执行", str(self.app.status_label["text"]))
        self.assertIn("上次执行：成功 3", str(self.app.result_label["text"]))
        self.reject_reuse()
        self.record("reopened-executed")
        self.app.on_undo()
        self.completed()

    def test_reopen_undone_history_displays_complete_without_auto_confirmation(self):
        self.execute().on_undo()
        data = self.immutable()
        self.reopen()
        self.completed()
        for p, content in data.items():
            self.assertEqual(p.read_bytes(), content)
        self.reject_reuse()
        self.record("reopened-completed")

    def test_reopen_partial_history_displays_partial_without_auto_confirmation(self):
        self.execute()
        self.block_sources(1)
        self.app.on_undo()
        self.reopen()
        self.assertIn("部分撤销", str(self.app.status_label["text"]))
        self.assertIn("已还原 2 · 被阻止 1", str(self.app.result_label["text"]))
        self.reject_reuse()
        self.record("reopened-partial")
        self.cleanup_partial()

    def test_history_journal_blocks_reuse_even_without_phase_counts(self):
        self.execute()
        Path(plan_state_path(self.sandbox.settings, self.app.plan["plan_id"])).unlink()
        self.reopen()
        self.assertIn("上次执行：成功 3", str(self.app.result_label["text"]))
        self.reject_reuse()
        self.record("history-missing-phase")
        self.app.on_undo()
        self.completed()

    def test_successful_new_preview_requires_confirmation_and_roundtrip(self):
        app = self.execute()
        app.on_undo()
        self.completed()
        old = app.plan["plan_id"]
        data = self.immutable()
        previous_statistics = str(app.result_label["text"])
        app.on_preview()
        self.assertNotEqual(app.plan["plan_id"], old)
        self.assertEqual(str(app.confirm_button["state"]), "normal")
        self.assertEqual(str(app.execute_button["state"]), "disabled")
        self.assertIsNone(app.confirmed_hash)
        self.assertEqual(str(app.result_label["text"]), previous_statistics)
        app.on_confirm()
        self.assertEqual(str(app.execute_button["state"]), "normal")
        app.on_execute()
        app.on_undo()
        self.completed()
        for p, content in data.items():
            self.assertEqual(p.read_bytes(), content)
        self.record("new-preview-roundtrip")

    def test_failed_new_preview_cannot_reactivate_undone_plan(self):
        app = self.execute()
        app.on_undo()
        old = app.plan["plan_id"]
        with patch.object(gui, "write_plan", side_effect=OSError("ARTIFICIAL write failure")):
            app.on_preview()
        self.assertEqual(app.plan["plan_id"], old)
        self.reject_reuse()
        self.completed()
        app.on_preview()
        app.on_confirm()
        app.on_execute()
        app.on_undo()
        self.completed()

    def test_skipped_execution_item_is_not_counted_as_restored(self):
        # Bind the conflict container before preview.  v3 persists every
        # target parent identity and must reject containers created afterwards.
        conflict_parent = self.sandbox.downloads / "下载分类" / "图片截图" / "Chosen" / "deep"
        conflict_parent.mkdir(parents=True, exist_ok=True)
        app = self.preview()
        conflict = conflict_parent / "图片.png"
        self.assertEqual(Path(app.plan["operations"][0]["dest"]), conflict)
        conflict.write_bytes(b"PREEXISTING-DESTINATION")
        conflict_identity = lstat_identity(str(conflict)).as_dict()
        app.on_confirm()
        app.on_execute()
        self.assertIn("成功 2 · 跳过 1", str(app.result_label["text"]))
        app.on_undo()
        self.completed(count=2)
        self.assertIn("无需还原 1", str(app.result_label["text"]))
        self.assertIdentityMatches(conflict, conflict_identity)
        self.assertEqual(conflict.read_bytes(), b"PREEXISTING-DESTINATION")
        self.record("skipped-item-not-restored")
