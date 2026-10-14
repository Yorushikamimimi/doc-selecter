"""自动发现新文件的回归测试。

要求（本轮验收）：

- 新文件发现、稳定等待、临时下载排除、分类目录排除；
- 自动检查始终**不移动、不自动确认、不修改旧计划**；
- 关闭/切换检查功能后无残留扫描任务；
- 用**可控时钟**验证定时逻辑，不靠长时间真实等待。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.harness import SandboxTestCase, require_gui_source

from doc_selecter.executor import execute_plan
from doc_selecter.planner import build_plan, write_confirmation, write_plan
from doc_selecter.scanner import collect_files
from doc_selecter.watcher import DiscoveryReport, NewFileWatcher


class _FakeClock:
    """可控时钟：测试显式推进时间，不做真实等待。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class WatcherDiscoveryTests(SandboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = _FakeClock()
        self.watcher = NewFileWatcher(settle_seconds=5.0, clock=self.clock)
        self.watcher.enable()

    def _check(self, **kwargs) -> DiscoveryReport:
        return self.watcher.check_once(
            downloads_dir=self.sandbox.settings.downloads_dir,
            target_parent_name=self.sandbox.settings.target_parent_name,
            **kwargs,
        )

    def test_new_file_is_settling_first_then_stable(self) -> None:
        self.sandbox.add_file("new.pdf", b"CONTENT")

        first = self._check()
        self.assertEqual(first.new_files, [])
        self.assertIn("new.pdf", first.settling)

        # 未到稳定时长 → 仍是「尚未稳定」
        self.clock.advance(2.0)
        second = self._check()
        self.assertEqual(second.new_files, [])
        self.assertIn("new.pdf", second.settling)

        # 超过稳定时长且大小/修改时间不变 → 报「新增待整理」
        self.clock.advance(4.0)
        third = self._check()
        self.assertEqual(third.new_files, ["new.pdf"])
        self.assertNotIn("new.pdf", third.settling)

        # 已报告过 → 不重复报告
        self.clock.advance(30.0)
        fourth = self._check()
        self.assertEqual(fourth.new_files, [])

    def test_growing_file_restarts_settle_timer(self) -> None:
        target = self.sandbox.add_file("growing.pdf", b"x")
        self._check()
        self.clock.advance(10.0)
        # 仍在写入：大小变化
        target.write_bytes(b"x" * 100)
        report = self._check()
        self.assertEqual(report.new_files, [], "仍在变化的文件不得报为新增")
        self.assertIn("growing.pdf", report.settling)
        # 稳定后才会报告
        self.clock.advance(6.0)
        self.assertEqual(self._check().new_files, ["growing.pdf"])

    def test_mtime_change_alone_restarts_settle_timer(self) -> None:
        target = self.sandbox.add_file("touch.pdf", b"x")
        self._check()
        self.clock.advance(10.0)
        st = target.stat()
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        report = self._check()
        self.assertEqual(report.new_files, [])
        self.assertIn("touch.pdf", report.settling)

    def test_temp_download_suffixes_are_excluded(self) -> None:
        self.sandbox.add_file("part.pdf.crdownload", b"x")
        self.sandbox.add_file("dl.part", b"x")
        self.sandbox.add_file("dl.tmp", b"x")
        self.sandbox.add_file("good.pdf", b"x")

        report = self._check()
        self.assertEqual(
            sorted(report.temp_skipped), ["dl.part", "dl.tmp", "part.pdf.crdownload"]
        )
        self.assertNotIn("part.pdf.crdownload", report.settling)

    def test_target_category_directory_is_excluded(self) -> None:
        self.sandbox.add_file("下载分类/文档资料/already.pdf", b"x")
        self.sandbox.add_file("fresh.pdf", b"x")
        report = self._check()
        self.assertNotIn("下载分类/文档资料/already.pdf", report.settling)
        self.assertIn("fresh.pdf", report.settling)

    def test_hidden_and_sensitive_are_not_reported(self) -> None:
        self.sandbox.add_file(".hidden.pdf", b"x")
        self.sandbox.add_file("id_rsa", b"x")
        self.sandbox.add_file("normal.pdf", b"x")
        report = self._check()
        names = set(report.settling) | set(report.new_files) | set(report.temp_skipped)
        self.assertNotIn(".hidden.pdf", names)
        self.assertNotIn("id_rsa", names)
        self.assertIn("normal.pdf", names)

    def test_known_paths_are_not_reported(self) -> None:
        self.sandbox.add_file("planned.pdf", b"x")
        self.clock.advance(1.0)
        self.watcher.mark_known(["planned.pdf"])
        report = self._check()
        self.assertNotIn("planned.pdf", report.settling)
        self.assertNotIn("planned.pdf", report.new_files)

    def test_scope_is_respected(self) -> None:
        self.sandbox.add_file("项目A/inside.pdf", b"x")
        self.sandbox.add_file("其他/outside.pdf", b"x")
        report = self._check(scope_dirs=["项目A"], recursive=True)
        names = set(report.settling) | set(report.new_files)
        self.assertIn("项目A/inside.pdf", names)
        self.assertNotIn("其他/outside.pdf", names)

    def test_removed_file_is_forgotten(self) -> None:
        target = self.sandbox.add_file("gone.pdf", b"x")
        self._check()
        target.unlink()
        self.clock.advance(10.0)
        report = self._check()
        self.assertEqual(report.settling, [])
        self.assertEqual(report.new_files, [])


class WatcherSafetyTests(SandboxTestCase):
    """自动检查绝不移动、不确认、不改计划。"""

    def test_check_never_moves_or_writes_state(self) -> None:
        self.sandbox.add_file("new.pdf", b"x")
        self.sandbox.add_file("top.pdf", b"y")
        settings = self.sandbox.settings
        plan = self.sandbox.make_plan()
        plan_bytes = self.sandbox.plan_file(plan["plan_id"]).read_bytes()
        confirmation = Path(settings.confirmations_dir) / f"{plan['plan_id']}.json"
        confirmation_bytes = confirmation.read_bytes()
        state_before = sorted(
            str(p.relative_to(self.sandbox.state)) for p in self.sandbox.state.rglob("*")
        )

        clock = _FakeClock()
        watcher = NewFileWatcher(settle_seconds=1.0, clock=clock)
        watcher.enable()
        for _ in range(3):
            clock.advance(5.0)
            watcher.check_once(
                downloads_dir=settings.downloads_dir,
                target_parent_name=settings.target_parent_name,
            )

        # 文件仍在原处
        self.assertTrue((self.sandbox.downloads / "new.pdf").is_file())
        self.assertTrue((self.sandbox.downloads / "top.pdf").is_file())
        # 未创建分类目录
        self.assertFalse((self.sandbox.downloads / "下载分类").exists())
        # 旧计划字节不变
        self.assertEqual(self.sandbox.plan_file(plan["plan_id"]).read_bytes(), plan_bytes)
        # 状态目录里没有新计划、没有确认记录变化
        plans = sorted(p.name for p in (self.sandbox.state / "plans").iterdir())
        self.assertEqual(plans, [f"{plan['plan_id']}.json"])
        self.assertEqual(confirmation.read_bytes(), confirmation_bytes,
                         "自动检查不得改写确认记录")
        state_after = sorted(
            str(p.relative_to(self.sandbox.state)) for p in self.sandbox.state.rglob("*")
        )
        self.assertEqual(state_after, state_before, "自动检查不得新增/删除任何状态文件")

    def test_check_does_not_execute_even_after_stable(self) -> None:
        self.sandbox.add_file("new.pdf", b"x")
        settings = self.sandbox.settings
        clock = _FakeClock()
        watcher = NewFileWatcher(settle_seconds=1.0, clock=clock)
        watcher.enable()
        clock.advance(1.0)
        watcher.check_once(downloads_dir=settings.downloads_dir,
                           target_parent_name=settings.target_parent_name)
        clock.advance(5.0)
        report = watcher.check_once(downloads_dir=settings.downloads_dir,
                                    target_parent_name=settings.target_parent_name)
        self.assertEqual(report.new_files, ["new.pdf"])
        self.assertTrue((self.sandbox.downloads / "new.pdf").is_file())
        self.assertFalse((self.sandbox.downloads / "下载分类").exists())

    def test_report_text_states_it_will_not_move(self) -> None:
        self.sandbox.add_file("a.pdf", b"x")
        clock = _FakeClock()
        watcher = NewFileWatcher(settle_seconds=1.0, clock=clock)
        watcher.enable()
        clock.advance(1.0)
        watcher.check_once(downloads_dir=self.sandbox.settings.downloads_dir,
                           target_parent_name=self.sandbox.settings.target_parent_name)
        clock.advance(5.0)
        report = watcher.check_once(downloads_dir=self.sandbox.settings.downloads_dir,
                                    target_parent_name=self.sandbox.settings.target_parent_name)
        text = NewFileWatcher.format_report(report)
        self.assertIn("不会自动移动或确认", text)


class WatcherLifecycleTests(SandboxTestCase):
    """开关切换与关闭后的状态。"""

    def test_disabled_by_default_and_no_discovery(self) -> None:
        self.sandbox.add_file("a.pdf", b"x")
        clock = _FakeClock()
        watcher = NewFileWatcher(settle_seconds=1.0, clock=clock)
        self.assertFalse(watcher.enabled)
        clock.advance(10.0)
        report = watcher.check_once(
            downloads_dir=self.sandbox.settings.downloads_dir,
            target_parent_name=self.sandbox.settings.target_parent_name,
        )
        self.assertEqual(report.new_files, [])
        self.assertEqual(report.settling, [])

    def test_disable_clears_tracking_state(self) -> None:
        self.sandbox.add_file("a.pdf", b"x")
        clock = _FakeClock()
        watcher = NewFileWatcher(settle_seconds=1.0, clock=clock)
        watcher.enable()
        watcher.check_once(downloads_dir=self.sandbox.settings.downloads_dir,
                           target_parent_name=self.sandbox.settings.target_parent_name)
        clock.advance(5.0)
        self.assertEqual(
            watcher.check_once(downloads_dir=self.sandbox.settings.downloads_dir,
                               target_parent_name=self.sandbox.settings.target_parent_name).new_files,
            ["a.pdf"],
        )
        watcher.disable()
        self.assertFalse(watcher.enabled)
        # 重新开启后不会把旧文件当成「已稳定新增」（跟踪状态已清空）
        watcher.enable()
        report = watcher.check_once(downloads_dir=self.sandbox.settings.downloads_dir,
                                    target_parent_name=self.sandbox.settings.target_parent_name)
        self.assertEqual(report.new_files, [])
        self.assertIn("a.pdf", report.settling)

    def test_reset_keep_reported_avoids_duplicate_prompts(self) -> None:
        self.sandbox.add_file("a.pdf", b"x")
        clock = _FakeClock()
        watcher = NewFileWatcher(settle_seconds=1.0, clock=clock)
        watcher.enable()
        watcher.check_once(downloads_dir=self.sandbox.settings.downloads_dir,
                           target_parent_name=self.sandbox.settings.target_parent_name)
        clock.advance(5.0)
        watcher.check_once(downloads_dir=self.sandbox.settings.downloads_dir,
                           target_parent_name=self.sandbox.settings.target_parent_name)
        watcher.reset(keep_reported=True)
        clock.advance(5.0)
        report = watcher.check_once(downloads_dir=self.sandbox.settings.downloads_dir,
                                    target_parent_name=self.sandbox.settings.target_parent_name)
        self.assertEqual(report.new_files, [], "已提示过的文件不应重复提示")
        watcher.reset()
        clock.advance(5.0)
        report2 = watcher.check_once(downloads_dir=self.sandbox.settings.downloads_dir,
                                     target_parent_name=self.sandbox.settings.target_parent_name)
        self.assertIn("a.pdf", report2.settling, "完全重置后重新开始观察")


class WatcherGuiSchedulingTests(SandboxTestCase):
    """GUI 调度：开关切换、关闭窗口后不残留 after 任务。"""

    def setUp(self) -> None:
        super().setUp()
        try:
            import tkinter as tk

            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:  # pragma: no cover - 环境相关
            self.skipTest(f"无可用显示：{exc}")

    def tearDown(self) -> None:
        try:
            if getattr(self, "root", None) is not None:
                self.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        super().tearDown()

    def _app(self):
        from doc_selecter import gui as gui_mod

        gui_mod.messagebox.showinfo = lambda *a, **k: None
        gui_mod.messagebox.showwarning = lambda *a, **k: None
        gui_mod.messagebox.showerror = lambda *a, **k: None
        gui_mod.messagebox.askokcancel = lambda *a, **k: True
        app = gui_mod.App(self.root, self.sandbox.settings)
        require_gui_source(app, self.sandbox.settings.downloads_dir)
        return gui_mod, app

    def test_toggle_schedules_and_cancels_job(self) -> None:
        gui_mod, app = self._app()
        app.watcher.interval_seconds = 0.05
        self.assertFalse(app.watcher.enabled)
        app._watch_enabled_var.set(True)
        app.on_toggle_watch()
        self.assertTrue(app.watcher.enabled)
        self.assertIsNotNone(app.watch_job, "开启后必须安排定时任务")

        app._watch_enabled_var.set(False)
        app.on_toggle_watch()
        self.assertFalse(app.watcher.enabled)
        self.assertIsNone(app.watch_job, "关闭后必须取消定时任务")

    def test_close_cancels_job_and_disables_watcher(self) -> None:
        gui_mod, app = self._app()
        app.watcher.interval_seconds = 0.05
        app._watch_enabled_var.set(True)
        app.on_toggle_watch()
        self.assertIsNotNone(app.watch_job)

        app.on_close()

        self.assertIsNone(app.watch_job, "关闭窗口后不得残留定时任务")
        self.assertFalse(app.watcher.enabled)

    def test_scope_change_resets_watcher_state(self) -> None:
        gui_mod, app = self._app()
        self.sandbox.add_file("项目A/a.pdf", b"x")
        app.on_preview()
        app.subfolder_var.set(True)
        app.selected_scope = ["项目A"]
        app._on_scope_changed()
        self.assertEqual(app.watcher._tracked, {}, "范围变化必须清空观察状态")

    def test_check_now_never_moves(self) -> None:
        gui_mod, app = self._app()
        self.sandbox.add_file("new.pdf", b"x")
        app.on_preview()
        plan_path = self.sandbox.plan_file(str(app.plan["plan_id"]))
        before = plan_path.read_bytes()
        app.watcher.set_clock(lambda: 100.0)
        app.watcher.set_settle_seconds(0.0)
        app.on_check_now()
        app.on_check_now()
        self.assertTrue((self.sandbox.downloads / "new.pdf").is_file())
        self.assertEqual(plan_path.read_bytes(), before, "自动检查不得修改旧计划")
        self.assertIn("仅提示", app.watch_text.get("1.0", "end"))


class CollectFilesTests(SandboxTestCase):
    """轻量枚举与完整扫描共享剔除规则。"""

    def test_collect_files_respects_scope_and_exclusions(self) -> None:
        self.sandbox.add_file("top.pdf", b"x")
        self.sandbox.add_file("项目A/a.pdf", b"x")
        self.sandbox.add_file("下载分类/文档资料/old.pdf", b"x")
        self.sandbox.add_file("密码备份/secret.txt", b"x")
        self.sandbox.add_file("CodeProj/package.json", b"{}")
        self.sandbox.add_file("CodeProj/index.js", b"x")

        top = collect_files(
            str(self.sandbox.downloads), target_parent_name="下载分类", recursive=False
        )
        self.assertEqual(sorted(top), ["top.pdf"])

        nested = collect_files(
            str(self.sandbox.downloads), target_parent_name="下载分类",
            scope_dirs=["项目A", "密码备份", "CodeProj"], recursive=True,
        )
        self.assertIn("项目A/a.pdf", nested)
        self.assertNotIn("密码备份/secret.txt", nested)
        self.assertNotIn("CodeProj/index.js", nested)
        self.assertNotIn("下载分类/文档资料/old.pdf", nested)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
