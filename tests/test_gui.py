"""GUI 验证：在沙盒内**实际启动** Tkinter 窗口并检查主要交互。

无可用显示时自动 skip（并在报告中如实标注为「未验证」）。

说明：Tkinter 在单元测试中反复创建/销毁 Tk 根窗口会导致进程崩溃，
因此这里**只建一个共享根窗口**，每个测试在自己的 ``Toplevel`` 里执行，
并且不依赖 ``update()``（直接查询控件状态即可）。
"""

from __future__ import annotations

import unittest

from tests.harness import SandboxTestCase, require_gui_source

from doc_selecter import gui as gui_mod

_SHARED_ROOT = None
_DISPLAY_ERROR = ""
try:  # pragma: no cover - 环境相关
    import tkinter as tk

    _SHARED_ROOT = tk.Tk()
    _SHARED_ROOT.withdraw()
except Exception as exc:  # pragma: no cover - 环境相关
    _DISPLAY_ERROR = str(exc)

DISPLAY_AVAILABLE = _SHARED_ROOT is not None


class GuiTests(SandboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        if not DISPLAY_AVAILABLE:
            self.skipTest(f"无可用显示，GUI 未验证：{DISPLAY_ERROR}")
        self.root = None

    def tearDown(self) -> None:
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:  # noqa: BLE001
                pass
        super().tearDown()

    def _stub_dialogs(self) -> list[tuple[str, str]]:
        """把所有模态对话框替换为记录器，避免测试阻塞在真实弹窗上。"""
        calls: list[tuple[str, str]] = []
        self._real_dialogs = {
            name: getattr(gui_mod.messagebox, name)
            for name in ("showinfo", "showwarning", "showerror", "askokcancel", "askyesno")
        }
        gui_mod.messagebox.showinfo = lambda t, m, **k: calls.append(("info", str(m)))
        gui_mod.messagebox.showwarning = lambda t, m, **k: calls.append(("warn", str(m)))
        gui_mod.messagebox.showerror = lambda t, m, **k: calls.append(("error", str(m)))
        gui_mod.messagebox.askokcancel = lambda t, m, **k: (calls.append(("ask", str(m))) or True)
        gui_mod.messagebox.askyesno = lambda t, m, **k: (calls.append(("ask", str(m))) or True)
        self.addCleanup(self._restore_dialogs)
        return calls

    def _restore_dialogs(self) -> None:
        for name, func in getattr(self, "_real_dialogs", {}).items():
            setattr(gui_mod.messagebox, name, func)

    def _build(self):
        """在共享根窗口下建一个 Toplevel 承载界面（避免反复创建/销毁 Tk 根）。"""
        self.dialog_calls = self._stub_dialogs()
        from doc_selecter.config import ensure_state_dirs

        settings = self.sandbox.settings
        ensure_state_dirs(settings)
        self.root = tk.Toplevel(_SHARED_ROOT)
        self.root.withdraw()
        app = gui_mod.App(self.root, settings)
        require_gui_source(app, settings.downloads_dir)
        return app

    def test_window_builds_with_chinese_labels_and_disabled_actions(self) -> None:
        app = self._build()
        # 标题在真实入口由 Tk 根窗口设置；此处界面挂在 Toplevel 上，改查其标题能力
        app.root.title(gui_mod.WINDOW_TITLE)
        self.assertEqual(self.root.title(), gui_mod.WINDOW_TITLE)
        # 初始：没有计划 → 三个动作按钮都不可用
        self.assertEqual(str(app.confirm_button["state"]), "disabled")
        self.assertEqual(str(app.execute_button["state"]), "disabled")
        self.assertEqual(str(app.undo_button["state"]), "disabled")
        self.assertIn("未生成预览", str(app.status_label["text"]))
        # 中文文案存在
        self.assertEqual(str(app.preview_button["text"]), "生成预览")
        self.assertEqual(str(app.execute_button["text"]), "执行移动")
        self.assertEqual(str(app.undo_button["text"]), "撤销最近一次整理")
        self.assertEqual(str(app.organize_button["text"]), "确认整理")
        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)

        labels = [
            str(w["text"]) for w in walk(app.root)
            if isinstance(w, gui_mod.ttk.Label)
        ]
        self.assertTrue(
            any("不保证无条件完整还原" in text for text in labels),
            f"界面必须展示撤销限制说明；实际标签：{labels}",
        )
        self.assertTrue(
            any("只读扫描" in text for text in labels), "预览按钮旁应说明只读语义"
        )

    def test_preview_then_confirmation_state_transitions(self) -> None:
        for name in ("a.pdf", "b.zip", "c.png"):
            self.sandbox.add_file(name, b"PAYLOAD")
        self.sandbox.add_dir("some-folder")
        self.sandbox.add_file("id_rsa", b"SECRET")  # 疑似敏感

        app = self._build()
        app.on_preview()

        self.assertIsNotNone(app.plan)
        self.assertIn("已预览", str(app.status_label["text"]))
        # 有明细行
        self.assertEqual(len(app.tree.get_children()), 3)
        # 跳过清单：敏感项只显示数量、不显示名称
        skip_text = app.skip_text.get("1.0", "end")
        self.assertIn("疑似敏感项", skip_text)
        self.assertIn("不展示名称", skip_text)
        self.assertNotIn("id_rsa", skip_text)
        # 分类总览有 7 个勾选框
        self.assertEqual(len(app._selection_vars), 7)
        # 未确认 → 可确认、不可执行
        self.assertEqual(str(app.confirm_button["state"]), "normal")
        self.assertEqual(str(app.execute_button["state"]), "disabled")

        app.on_confirm()
        self.assertIn("已确认", str(app.status_label["text"]))
        self.assertEqual(str(app.execute_button["state"]), "normal")
        original_hash = app.confirmed_hash
        self.assertEqual(original_hash, app.plan["plan_hash"])

        # 改动勾选 → 确认失效
        app._selection_vars["文档资料"].set(False)
        app.on_toggle_category()
        self.assertIsNone(app.confirmed_hash, "勾选变化必须使确认失效")
        self.assertEqual(str(app.execute_button["state"]), "disabled")
        self.assertIn("需重新确认", str(app.hash_label["text"]))

    def test_execute_and_undo_through_ui_with_stubbed_dialogs(self) -> None:
        for name in ("a.pdf", "b.zip"):
            self.sandbox.add_file(name, b"PAYLOAD-" + name.encode())
        app = self._build()
        app.on_preview()
        app.on_confirm()

        calls = self.dialog_calls
        app.on_execute()
        self.assertFalse(any(k == "error" for k, _ in calls), calls)
        self.assertIn("已执行", str(app.status_label["text"]))
        self.assertIn("成功 2", str(app.result_label["text"]))
        self.assertNotExists(self.sandbox.downloads / "a.pdf")
        self.assertTrue((self.sandbox.target_dir("文档资料") / "PDF" / "a.pdf").is_file())
        self.assertEqual(str(app.undo_button["state"]), "normal")

        app.on_undo()
        self.assertTrue(any("已还原" in m for k, m in calls if k == "info"), calls)
        self.assertExists(self.sandbox.downloads / "a.pdf")
        self.assertExists(self.sandbox.downloads / "b.zip")

    def test_execute_button_without_confirmation_is_refused(self) -> None:
        self.sandbox.add_file("a.pdf", b"PAYLOAD")
        app = self._build()
        app.on_preview()

        app.on_execute()
        warnings = [m for k, m in self.dialog_calls if k == "warn"]
        self.assertTrue(warnings and "尚未确认" in warnings[0], self.dialog_calls)
        self.assertExists(self.sandbox.downloads / "a.pdf")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
