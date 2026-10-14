"""真实可见 GUI 与 mainloop 冒烟验证（独立子进程）。

**准确的能力边界（主控复验后修正表述）**：

- 本文件在**独立子进程**里用产品入口 ``doc_selecter.gui.build_app`` 建真实 Tk 根窗口、
  断言 ``winfo_ismapped``/``winfo_viewable`` 与真实几何，并**真正运行** ``root.mainloop()``
  若干轮（``after`` tick）；
- **预览、确认与移动都在 ``mainloop`` 之前完成**：因此可称「控件真实映射 + ``update``
  与 ``mainloop`` 冒烟」，**不能**称为「事件循环内的完整用户交互」，也**不能**称为
  「原生弹窗通过」（本文件完全不经过 ``tk_messageBox``）。

为什么必须用子进程：本套件为了与单元测试安全共存，采用「一个共享 Tk 根 + 每个用例
一个 Toplevel、绝不 destroy 根」的模式，被固定的根窗口无法安全运行 ``mainloop``。

**未验证（环境限制，不扩大系统权限）**：像素级截图（``screencapture`` 报
"could not create image from display"）、真实鼠标点击、原生确认弹窗人工验收
（``osascript`` 报 "不允许发送按键"；macOS 原生模态不返回 Tcl 事件循环）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from tests.harness import SandboxTestCase

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DRIVER = textwrap.dedent(
    """
    import json, os, sys, time
    sys.path.insert(0, __PROJECT__)
    from doc_selecter import gui as gui_mod
    from doc_selecter.config import load_settings, ensure_state_dirs
    from doc_selecter.planner import write_confirmation
    from tests.harness import require_gui_source, require_gui_preview

    report = {}
    settings = load_settings()
    try:
        ensure_state_dirs(settings)
        root, app = gui_mod.build_app(settings)
        require_gui_source(app, settings.downloads_dir)
        root.deiconify(); root.lift(); root.attributes("-topmost", True)
        root.update_idletasks(); root.update()
        report["title"] = root.title()
        report["mapped"] = bool(root.winfo_ismapped())
        report["viewable"] = bool(root.winfo_viewable())
        report["geometry"] = [root.winfo_width(), root.winfo_height()]
        report["screen"] = [root.winfo_screenwidth(), root.winfo_screenheight()]

        # 真实 UI 路径：生成预览 → 确认 → 执行（不经过任何桩）
        require_gui_preview(app)
        root.update()
        report["tree_rows"] = len(app.tree.get_children())
        report["status_after_preview"] = str(app.status_label["text"])
        report["plan_id"] = str(app.plan["plan_id"]) if app.plan else None
        report["plan_hash"] = str(app.plan["plan_hash"]) if app.plan else None
        report["categories"] = len(app._selection_vars)
        # 本轮新增控件：包含子文件夹 / 选择文件夹 / 自动检查开关 / 立即检查
        report["subfolder_control"] = str(app.subfolder_var.get())
        report["pick_button_state"] = str(app.pick_button["state"])
        report["scope_label"] = str(app.scope_label["text"])
        report["watch_control"] = str(app._watch_enabled_var.get())
        report["watch_now_button"] = str(app.watch_now_button["text"])
        report["watch_text_initially_hidden"] = not bool(app.watch_text.winfo_viewable())
        report["settings_initially_hidden"] = app.settings_window.state() == "withdrawn"
        app.on_toggle_settings()
        root.update_idletasks(); root.update()
        report["watch_widget_geometry"] = {
            "pick_button": app.pick_button.winfo_width(),
            "watch_check": app.watch_check.winfo_width(),
            "watch_now_button": app.watch_now_button.winfo_width(),
        }
        report["watch_widgets_ismapped"] = {
            "pick_button": bool(app.pick_button.winfo_ismapped()),
            "watch_check": bool(app.watch_check.winfo_ismapped()),
            "watch_now_button": bool(app.watch_now_button.winfo_ismapped()),
        }
        # 打开自动检查（仅提示），确认安排了定时任务且没有移动
        app._watch_enabled_var.set(True)
        app.on_toggle_watch()
        report["watch_enabled"] = bool(app.watcher.enabled)
        report["watch_job_scheduled"] = app.watch_job is not None
        report["watch_label"] = str(app.watch_label["text"])
        app.on_check_now()
        report["watch_text_head"] = app.watch_text.get("1.0", "end").strip().splitlines()[0]
        report["downloads_listing_after_check"] = sorted(
            os.listdir(settings.downloads_dir)
        )
        app._watch_enabled_var.set(False)
        app.on_toggle_watch()
        report["watch_job_after_off"] = app.watch_job
        report["watch_enabled_after_off"] = bool(app.watcher.enabled)
        # 折叠/展开子文件夹控件后确认状态：勾选「包含子文件夹」应使选择按钮可用
        app.subfolder_var.set(True)
        app.on_toggle_subfolders()
        report["pick_button_after_toggle"] = str(app.pick_button["state"])

        # 确认：写真实确认记录并让界面进入「已确认」（绕开无法自动关闭的原生模态弹窗）
        write_confirmation(settings, app.plan["plan_id"], app.plan["plan_hash"])
        app.confirmed_hash = str(app.plan["plan_hash"])
        app._set_status(gui_mod.STATE_CONFIRMED)
        app._refresh_plan_buttons()
        root.update()
        report["status_after_confirm"] = str(app.status_label["text"])
        report["execute_button"] = str(app.execute_button["state"])
        report["confirmed"] = app.confirmed_hash is not None

        # 改动勾选 → 确认失效（真实控件状态）
        app._selection_vars["文档资料"].set(False)
        app.on_toggle_category()
        root.update()
        report["execute_after_toggle"] = str(app.execute_button["state"])
        report["confirm_invalidated"] = app.confirmed_hash is None
        # 恢复勾选并重新确认
        app._selection_vars["文档资料"].set(True)
        app.on_toggle_category()  # 新集合需要通过实际回调生成新计划
        write_confirmation(settings, app.plan["plan_id"], app.plan["plan_hash"])
        app.confirmed_hash = str(app.plan["plan_hash"])
        app._refresh_plan_buttons()
        root.update()

        # 执行：直接走执行器（真实模态弹窗在 macOS 上阻塞 Tcl 循环，无法自动化关闭）
        result = app.perform_execution()   # 走界面自身的真实执行路径（不含模态弹窗）
        app._set_status(gui_mod.STATE_EXECUTED)
        root.update()
        report["moved"] = result.moved
        report["result_label"] = str(app.result_label["text"])
        report["status_after_execute"] = str(app.status_label["text"])

        # 真实 mainloop：跑若干轮事件后退出
        def finish():
            report["mainloop_ticks"] = report.get("mainloop_ticks", 0) + 1
            if report["mainloop_ticks"] >= 3:
                root.quit()
            else:
                root.after(60, finish)

        root.after(60, finish)
        root.mainloop()
        root.destroy()
        report["ok"] = True
    except BaseException as exc:
        import traceback
        report["ok"] = False
        report["error"] = traceback.format_exc()
    print("REPORT " + json.dumps(report, ensure_ascii=False))
    """
)


class GuiMainloopSmokeTest(SandboxTestCase):
    def test_real_window_preview_confirm_execute_in_mainloop(self) -> None:
        self.sandbox.add_file("report.pdf", b"PAYLOAD")
        script = DRIVER.replace("__PROJECT__", repr(str(PROJECT_ROOT)))
        env = {**os.environ}
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, env=env, timeout=90,
        )
        payload = None
        for line in proc.stdout.splitlines():
            if line.startswith("REPORT "):
                payload = json.loads(line[len("REPORT "):])
        self.assertIsNotNone(
            payload,
            f"子进程未输出报告；rc={proc.returncode}\nstdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}",
        )
        assert payload is not None
        self.assertTrue(payload.get("ok"), f"子进程失败：{payload.get('error')}")
        self.assertEqual(payload["title"], "文件整理助手")
        self.assertTrue(payload["mapped"], "根窗口必须真实映射")
        self.assertTrue(payload["viewable"], "根窗口必须可见")
        self.assertGreaterEqual(payload["geometry"][0], 900)
        self.assertGreaterEqual(payload["geometry"][1], 600)
        self.assertEqual(payload["tree_rows"], 1, "预览应展示 1 行明细")
        self.assertIn("已预览", payload["status_after_preview"])
        self.assertEqual(payload["categories"], 7)
        self.assertIn("已确认", payload["status_after_confirm"])
        self.assertEqual(payload["execute_button"], "normal")
        self.assertTrue(payload["confirmed"])
        # 改动勾选 → 确认失效（真实控件状态）
        self.assertEqual(payload["execute_after_toggle"], "disabled")
        self.assertTrue(payload["confirm_invalidated"])
        # 真实执行
        self.assertEqual(payload["moved"], 1)
        self.assertIn("成功 1", payload["result_label"])
        self.assertIn("已执行", payload["status_after_execute"])
        self.assertTrue((self.sandbox.target_dir("文档资料") / "PDF" / "report.pdf").is_file(),
                        "真实 UI 驱动后文件必须到达目标目录")
        self.assertGreaterEqual(payload.get("mainloop_ticks", 0), 3, "必须真正跑过 mainloop")
        # 本轮新增控件的可见性与行为
        self.assertEqual(payload["subfolder_control"], "False")
        self.assertEqual(payload["pick_button_state"], "disabled")
        self.assertIn("仅顶层", payload["scope_label"])
        self.assertEqual(payload["watch_control"], "False")
        self.assertEqual(payload["watch_now_button"], "立即检查")
        self.assertTrue(payload["watch_text_initially_hidden"])
        self.assertTrue(payload["settings_initially_hidden"])
        geometry = payload["watch_widget_geometry"]
        for name, size in geometry.items():
            self.assertGreater(size, 1, f"新控件 {name} 必须完成真实布局：{geometry}")
        self.assertTrue(payload["watch_enabled"])
        self.assertTrue(payload["watch_job_scheduled"], "开启自动检查必须安排 after 任务")
        self.assertIn("仅提示", payload["watch_label"])
        self.assertIn("仅提示", payload["watch_text_head"])
        self.assertIn("report.pdf", payload["downloads_listing_after_check"])
        self.assertIsNone(payload["watch_job_after_off"], "关闭自动检查后不得残留任务")
        self.assertFalse(payload["watch_enabled_after_off"])
        self.assertEqual(payload["pick_button_after_toggle"], "normal")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
