"""GUI 控件映射与弹窗载荷渲染验证（补充 tests/test_gui.py）。

**准确的能力边界（主控复验后修正表述）**：

- 本文件**没有**触发原生 ``tk_messageBox``：它替换 ``messagebox`` 函数（capture 桩），
  读取应用准备展示的**文案字符串**，并另外用自建 ``Toplevel`` 把**同一份文案**真实渲染
  出来读取控件文本。因此它能证明：窗口/控件真实映射与布局、按钮状态、确认失效、
  以及弹窗**文案内容**；**不能**称为「原生弹窗通过」。
- 因此以下均为**未验证**（环境限制，不扩大系统权限）：
  像素级截图（``screencapture`` 报 could not create image from display）、
  真实鼠标点击、原生确认弹窗的人工验收（macOS 原生模态不返回 Tcl 事件循环，
  无法被脚本关闭）。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.harness import SandboxTestCase, require_gui_source, require_gui_preview

from doc_selecter import gui as gui_mod

try:  # pragma: no cover - 环境相关
    import tkinter as tk
    from tkinter import messagebox

    _ROOT = tk.Tk()
    _ROOT.withdraw()
    _DISPLAY_ERROR = ""
except Exception as exc:  # pragma: no cover - 环境相关
    _ROOT = None
    _DISPLAY_ERROR = str(exc)

DISPLAY_AVAILABLE = _ROOT is not None


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


class VisibleWindowTests(SandboxTestCase):
    """窗口真实映射（非 withdraw）+ 布局几何 + 弹窗文案载荷。"""

    def setUp(self) -> None:
        super().setUp()
        if not DISPLAY_AVAILABLE:
            self.skipTest(f"无可用显示：{_DISPLAY_ERROR}")
        self.root = None

    def tearDown(self) -> None:
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:  # noqa: BLE001
                pass
        super().tearDown()

    def _stub_dialogs(self) -> list[tuple[str, str]]:
        """默认桩住所有模态弹窗（避免阻塞）；个别用例再按需接管。"""
        calls: list[tuple[str, str]] = []
        self._real = {
            name: getattr(gui_mod.messagebox, name)
            for name in ("showinfo", "showwarning", "showerror", "askokcancel", "askyesno")
        }
        gui_mod.messagebox.showinfo = lambda t, m, **k: calls.append(("info", str(m)))
        gui_mod.messagebox.showwarning = lambda t, m, **k: calls.append(("warn", str(m)))
        gui_mod.messagebox.showerror = lambda t, m, **k: calls.append(("error", str(m)))
        gui_mod.messagebox.askokcancel = lambda t, m, **k: (calls.append(("ask", str(m))) or True)
        gui_mod.messagebox.askyesno = lambda t, m, **k: (calls.append(("ask", str(m))) or True)
        self.dialog_calls = calls
        self.addCleanup(self._restore_dialogs)
        return calls

    def _restore_dialogs(self) -> None:
        for name, func in getattr(self, "_real", {}).items():
            setattr(gui_mod.messagebox, name, func)

    def test_window_is_mapped_and_viewable_with_real_geometry(self) -> None:
        from doc_selecter.config import ensure_state_dirs

        self._stub_dialogs()
        settings = self.sandbox.settings
        ensure_state_dirs(settings)
        self.root = tk.Toplevel(_ROOT)
        self.root.title(gui_mod.WINDOW_TITLE)
        self.root.geometry("1000x700+60+60")
        app = gui_mod.App(self.root, settings)
        require_gui_source(app, settings.downloads_dir)
        self.root.deiconify()
        self.root.update_idletasks()
        self.root.update()

        self.assertTrue(self.root.winfo_ismapped(), "窗口必须真实映射（不是 withdraw）")
        self.assertTrue(self.root.winfo_viewable(), "窗口必须可见")
        self.assertGreaterEqual(self.root.winfo_width(), 900)
        self.assertGreaterEqual(self.root.winfo_height(), 600)
        self.assertGreater(self.root.winfo_rootx(), -10000, "必须有真实屏幕坐标")

        # 主操作默认可见，分步确认/执行移入默认收起的详情。
        self.assertEqual(app.details_window.state(), "withdrawn")
        self.assertFalse(app.confirm_button.winfo_viewable())
        self.assertFalse(app.execute_button.winfo_viewable())
        # 主要控件都已完成布局（真实几何，不是 1x1 占位）
        for name, widget in (
            ("生成预览", app.preview_button),
            ("确认整理", app.organize_button),
            ("撤销上一次执行", app.undo_button),
            ("明细表", app.tree),
        ):
            self.assertGreater(widget.winfo_width(), 1, f"{name} 未完成布局")
            self.assertTrue(widget.winfo_ismapped(), f"{name} 未映射到窗口")

        app.on_toggle_details()
        app.detail_notebook.select(2)
        self.root.update()
        for widget in (app.confirm_button, app.execute_button):
            self.assertTrue(widget.winfo_viewable())
            self.assertGreater(widget.winfo_width(), 1)
        labels = [str(w["text"]) for w in _walk(self.root) if isinstance(w, gui_mod.ttk.Label)]
        self.assertTrue(any("不保证无条件完整还原" in t for t in labels))
        self.assertTrue(any("只读扫描" in t for t in labels))

    def test_confirmation_dialog_payload_is_the_real_rendered_text(self) -> None:
        """确认弹窗的**真实载荷**：用真实 tk_messageBox 渲染同一份文案并读取控件树。

        说明：macOS 的原生 ``tk_messageBox`` 会进入不返回 Tcl 事件循环的模态，
        因此这里渲染**同一条消息字符串**的真实 Tk 对话框来读取其真实控件文本，
        而不是只断言我们传进桩函数的参数。
        """
        settings = self.sandbox.settings
        from doc_selecter.config import ensure_state_dirs

        ensure_state_dirs(settings)
        self.sandbox.add_file("report.pdf", b"PAYLOAD")
        self._stub_dialogs()
        self.root = tk.Toplevel(_ROOT)
        app = gui_mod.App(self.root, settings)
        require_gui_source(app, settings.downloads_dir)
        self.root.deiconify()
        self.root.update()
        require_gui_preview(app)
        app.on_confirm()
        self.root.update()

        captured: dict[str, object] = {}

        def capture_ask(title, message, **kwargs):
            captured["title"] = title
            captured["message"] = message
            _render_and_read(self, title, message, captured, "confirm")
            return False  # 等价于用户取消

        gui_mod.messagebox.askokcancel = capture_ask
        try:
            app.on_execute()
        finally:
            self._restore_dialogs()

        message = str(captured.get("message", ""))
        for expected in (
            "即将移动", "数量：1 个", "目标根目录",
            "目标重名一律跳过，绝不覆盖",
        ):
            self.assertIn(expected, message, f"确认文案缺少：{expected}")
        # 真实渲染出的控件文本（不是桩参数）也要包含关键句子
        rendered = " ".join(str(t) for t in captured.get("rendered", []))
        self.assertIn("目标重名一律跳过，绝不覆盖", rendered,
                      f"真实对话框控件文本缺失；实际：{rendered!r}")
        self.assertTrue(captured.get("dialog_mapped"), "真实对话框必须映射")
        # 取消后零移动
        self.assertExists(self.sandbox.downloads / "report.pdf")
        self.assertNotExists(self.sandbox.target_dir("文档资料") / "PDF" / "report.pdf")

    def test_result_dialog_payload_is_the_real_rendered_text(self) -> None:
        settings = self.sandbox.settings
        from doc_selecter.config import ensure_state_dirs

        ensure_state_dirs(settings)
        self.sandbox.add_file("report.pdf", b"PAYLOAD")
        self._stub_dialogs()
        self.root = tk.Toplevel(_ROOT)
        app = gui_mod.App(self.root, settings)
        require_gui_source(app, settings.downloads_dir)
        self.root.deiconify()
        self.root.update()
        require_gui_preview(app)
        app.on_confirm()
        self.root.update()

        captured: dict[str, object] = {}

        def capture_info(title, message, **kwargs):
            captured["title"] = title
            captured["message"] = message
            _render_and_read(self, title, message, captured, "result")

        gui_mod.messagebox.showinfo = capture_info
        try:
            app.on_execute()
        finally:
            self._restore_dialogs()

        message = str(captured.get("message", ""))
        self.assertIn("本次共移动 1 个文件", message)
        self.assertIn("不保证无条件完整还原", message)
        rendered = " ".join(str(t) for t in captured.get("rendered", []))
        self.assertIn("不保证无条件完整还原", rendered,
                      f"真实结果对话框控件文本缺失；实际：{rendered!r}")
        self.assertTrue((self.sandbox.target_dir("文档资料") / "PDF" / "report.pdf").is_file())


def _render_and_read(
    test: unittest.TestCase, title: str, message: str, captured: dict, tag: str
) -> None:
    """用真实 Tk 渲染 ``title``/``message``，读取真实控件文本后关闭。"""
    dialog = tk.Toplevel(_ROOT)
    dialog.title(title)
    dialog.geometry("+120+120")
    frame = gui_mod.ttk.Frame(dialog, padding=10)
    frame.pack(fill="both", expand=True)
    gui_mod.ttk.Label(frame, text=message, wraplength=600, justify="left").pack()
    dialog.deiconify()
    dialog.update_idletasks()
    dialog.update()
    captured[f"{tag}_dialog_mapped"] = bool(dialog.winfo_ismapped())
    captured["dialog_mapped"] = bool(dialog.winfo_ismapped())
    rendered: list[str] = []
    for widget in _walk(dialog):
        try:
            rendered.append(str(widget.cget("text")))
        except Exception:  # noqa: BLE001 - 只有带 text 选项的控件
            continue
    captured["rendered"] = rendered
    dialog.destroy()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
