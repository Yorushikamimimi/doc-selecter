"""中文 Tkinter 界面（单窗口）。

界面与执行逻辑严格分离：

- **生成预览**：只读扫描，不持锁、不移动任何文件；
- **执行移动**：需要二次确认对话框（列出待移动数量、逻辑总大小、目标根目录）；
- **改动即失效**：勾选变化会丢弃当前确认，必须重新确认；
- **撤销上一次执行**：需要再次确认，并明确提示「不保证无条件完整还原」。
"""

from __future__ import annotations

import os
import copy
import queue
import threading
import subprocess
import stat
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .config import ConfigError, Settings, assert_downloads_exists, ensure_state_dirs, load_settings
from .executor import ExecutionRefused, execute_plan, recover_execution
from .locking import LockBusy, current_phase, read_plan_state
from .journal import JournalCorrupted, JournalNeedsRepair
from .planner import (
    PlanError,
    assert_new_move_admissible,
    build_plan,
    list_plans,
    load_plan,
    load_verified_plan,
    public_plan_view,
    write_confirmation,
    write_plan,
)
from .rules import CATEGORIES, redact_private_text, document_subcategory, normalize_extension
from .scanner import list_selectable_folders, normalize_scope
from .plan_hash import normalize_path
from .snapshot import fold_verified_journals, read_snapshot
from .undo import UndoRefused, undo_plan
from .watcher import DEFAULT_INTERVAL_SECONDS, NewFileWatcher
from .gui_source import selected_settings
from .gui_session import OperationSession, latest_restorable_plan

WINDOW_TITLE = "文件整理助手"
FOOTER_WARNING = "撤销仅在条件满足时生效，不保证无条件完整还原。"
FOOTER_HINT = "确认前不会移动任何文件 · 同名文件不覆盖"

STATE_IDLE = "未生成预览"
STATE_PREVIEWED = "已预览（待确认）"
STATE_CONFIRMED = "已确认（可执行）"
STATE_EXECUTED = "已执行（可撤销）"
STATE_UNDONE = "已撤销"
STATE_NEEDS_REVIEW = "需检查（撤销未完成或记录异常）"
STATE_ERROR = "出错"

#: 自动检查的默认间隔（秒），可用 ``--watch-interval`` 语义的构造参数覆盖（测试用）
DEFAULT_WATCH_INTERVAL = DEFAULT_INTERVAL_SECONDS


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


class App:
    """主窗口。"""

    def __init__(self, root: tk.Tk, settings: Settings) -> None:
        self.root = root
        self.settings = settings
        self._base_settings = settings
        self._source_selected = False
        self._operation_busy = False
        self._remaining_epoch = 0
        self._remaining_dialog = None
        self.session = OperationSession()
        self._chain_result = None
        self.plan: dict[str, Any] | None = None
        self.confirmed_hash: str | None = None
        self.archive_var = tk.BooleanVar(value=False)
        # 历史计划仍可查看/撤销；确认准入只授予成功保存并匹配当前选择的计划。
        self._preview_ready = False
        self._selection_vars: dict[str, tk.BooleanVar] = {}
        # 扫描范围：默认仅顶层；用户勾选「包含子文件夹」并选择后才递归
        self.include_subfolders = False
        self.selected_scope: list[str] = []
        # 自动检查新文件（默认关闭，仅在 GUI 运行期间用 after 调度）
        self.watcher = NewFileWatcher(clock=self._clock)
        self.watch_job: str | None = None
        self._watch_enabled_var: tk.BooleanVar | None = None
        self._closing = False
        self._init_discovery_worker()

        root.title(WINDOW_TITLE)
        root.geometry("1100x760")
        root.minsize(900, 720)

        self._build_header()
        self._build_body()
        self._build_footer()
        self._refresh_plan_buttons()
        # 关闭窗口时清理定时任务，避免残留扫描
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # --- 时钟（测试可覆盖，避免真实等待） ------------------------------------
    def _clock(self) -> float:
        import time

        return time.monotonic()

    # --- 界面构建 -----------------------------------------------------------
    def _build_header(self) -> None:
        self._view_category = None
        self._history_view = {"consumed": False}
        self._tooltip = None
        self._tooltip_job = None
        style = ttk.Style(self.root)
        # ttk 控件在 Aqua 下会用样式背景色铺满自身矩形；与所在区域同色才不出现灰底块。
        for name, color in (("Side", "#f1f2f5"), ("SideSel", "#e4e4ea"), ("Main", "#ffffff"),
                            ("Tool", "#f7f7f8"), ("Banner", "#e5f4ec")):
            for widget in ("TFrame", "TLabel", "TButton", "TCheckbutton"):
                style.configure(f"{name}.{widget}", background=color)
        style.configure("Files.Treeview", rowheight=32, font=("", 12))
        style.configure("Files.Treeview.Heading", font=("", 12, "bold"))
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.sidebar = tk.Frame(self.root, background="#f1f2f5", width=232, padx=12, pady=12)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(4, weight=1)
        source = tk.Frame(self.sidebar, padx=4, pady=4, background="#f1f2f5")
        source.grid(row=0, column=0, sticky="ew", pady=(2, 8))
        self.folder_name_label = tk.Label(source, text="尚未选择文件夹", background="#f1f2f5",
                                          font=("", 14, "bold"), wraplength=190)
        self.folder_name_label.pack(anchor="w")
        self.scope_label = tk.Label(source, text="扫描范围：仅顶层", background="#f1f2f5",
                                    foreground="#6e6e73", wraplength=190)
        self.scope_label.pack(anchor="w", pady=4)
        self.source_button = ttk.Button(source, text="更换…", command=self.on_pick_source, style="Side.TButton")
        self.source_button.pack(anchor="w")
        scope = tk.Frame(self.sidebar, background="#f1f2f5")
        scope.grid(row=1, column=0, sticky="ew")
        self.subfolder_var = tk.BooleanVar(value=False)
        self.subfolder_check = ttk.Checkbutton(scope, text="包含子文件夹", variable=self.subfolder_var,
                                               style="Side.TCheckbutton", command=self.on_toggle_subfolders)
        self.subfolder_check.pack(anchor="w")
        self.pick_button = ttk.Button(scope, text="选择子文件夹…", command=self.on_pick_folders, state="disabled",
                                      style="Side.TButton")
        self.pick_button.pack(anchor="w", pady=(4, 8))
        self.category_title = tk.Label(self.sidebar, text="本次整理的类别", background="#f1f2f5", foreground="#7a7e87")
        self.category_title.grid(row=2, column=0, sticky="w", pady=(10, 6))
        self.overview_frame = tk.Frame(self.sidebar, background="#f1f2f5")
        self.overview_frame.grid(row=3, column=0, sticky="ew")
        # 只在确有跳过项时显示（见 _sync_category_rows）。
        self.skip_button = ttk.Button(self.sidebar, text="跳过 0 项 · 看原因", command=self.on_show_skips,
                                      style="Side.TButton")
        self.main_frame = tk.Frame(self.root, background="#ffffff")
        self.main_frame.grid(row=0, column=1, sticky="nsew")
        self.main_frame.columnconfigure(0, weight=1)
        self.main_frame.rowconfigure(2, weight=1)
        frame = tk.Frame(self.main_frame, padx=16, pady=12, background="#f7f7f8")
        frame.grid(row=0, column=0, sticky="ew")
        self.toolbar_title = tk.Label(frame, text="整理文件", background="#f7f7f8", font=("", 15, "bold"))
        self.toolbar_title.pack(side="left")
        self.mode_label = tk.Label(frame, text="未预览", padx=8, pady=2, background="#ececf0", foreground="#6e6e73")
        self.mode_label.pack(side="left", padx=10)
        self.settings_button = ttk.Button(frame, text="设置", command=self.on_toggle_settings, style="Tool.TButton")
        self.settings_button.pack(side="right")
        self.operations_button = ttk.Button(frame, text="操作记录", command=self.on_toggle_operations,
                                            style="Tool.TButton")
        self.operations_button.pack(side="right", padx=6)
        self.details_button = ttk.Button(frame, text="详情", command=self.on_toggle_details, style="Tool.TButton")
        self.details_button.pack(side="right")
        self.banner = tk.Frame(self.main_frame, background="#e8efff", padx=16, pady=10)
        self.banner.columnconfigure(0, weight=1)
        self.banner_heading = tk.Frame(self.banner, background="#e8efff")
        self.banner_heading.grid(row=0, column=0, sticky="ew")
        self.banner_title = tk.Label(self.banner_heading, font=("", 13, "bold"), wraplength=420, anchor="w", justify="left")
        self.banner_title.pack(side="left", fill="x", expand=True)
        self.finder_button = ttk.Button(self.banner_heading, text="在访达中打开", command=self.on_open_target,
                                        style="Banner.TButton")
        self.problem_label = tk.Label(self.banner, text="", wraplength=580, justify="left", anchor="w")
        self.problem_label.grid(row=1, column=0, sticky="ew")

    def _aux_window(self, title: str) -> tk.Toplevel:
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.title(title)
        window.geometry("900x680")
        window.minsize(720, 560)
        window.transient(self.root)
        return window

    def _build_body(self) -> None:
        body = tk.Frame(self.main_frame, background="#ffffff")
        self.body_frame = body
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        review = tk.Frame(body, background="#ffffff")
        self.review_frame = review
        review.grid(row=0, column=0, sticky="nsew")
        review.columnconfigure(0, weight=1)
        review.rowconfigure(0, weight=1)
        # Keep the original five detail values; presentation columns are appended.
        columns = ("rel", "source", "dest", "basis", "size", "folder", "format", "result")
        self.tree = ttk.Treeview(review, columns=columns, displaycolumns=("rel", "size", "folder"),
                                 show="headings", height=10, style="Files.Treeview")
        for key, title in (("rel", "文件"), ("size", "大小"), ("folder", "整理到"),
                           ("format", "格式"), ("result", "结果")):
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=150, minwidth=50, anchor="w", stretch=False)
        self.tree.tag_configure("skipped", background="#fff3dc", foreground="#9a5b00")
        self.tree.tag_configure("even", background="#fafbfc")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(review, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(review, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.bind("<Configure>", self._fit_file_columns)
        self.tree.bind("<Motion>", self._file_hover)
        self.tree.bind("<Leave>", self._hide_tooltip)
        self.empty_frame = tk.Frame(body, background="#ffffff")
        self.empty_frame.columnconfigure(0, weight=1)
        self.empty_frame.rowconfigure(0, weight=1)
        self.empty_frame.rowconfigure(2, weight=1)
        center = tk.Frame(self.empty_frame, padx=20, pady=20, background="#ffffff")
        center.grid(row=1, column=0)
        self.empty_icon = tk.Canvas(center, width=52, height=44, background="#ffffff",
                                    highlightthickness=0, borderwidth=0)
        self.empty_icon.pack(pady=(0, 10))
        self.empty_title = tk.Label(center, font=("", 17, "bold"), background="#ffffff", wraplength=420)
        self.empty_title.pack(pady=6)
        self.empty_description = tk.Label(center, background="#ffffff", foreground="#5d6068",
                                           wraplength=380, justify="center")
        self.empty_description.pack(pady=(0, 16))
        self.empty_action = ttk.Button(center, text="选择文件夹…", command=self.on_pick_source, default="active",
                                       style="Main.TButton")
        self.empty_action.pack()

        self.details_window = self._aux_window("整理详情")
        self.details_window.protocol("WM_DELETE_WINDOW", self.on_toggle_details)
        info = ttk.Frame(self.details_window, padding=12)
        info.pack(fill="x")
        self.source_label = ttk.Label(info, text="整理位置：尚未选择 · 请先选择整理文件夹", wraplength=850)
        self.source_label.pack(anchor="w")
        self.target_label = ttk.Label(info,
            text=f"按类型整理到上述文件夹内的「{self._public(self.settings.target_parent_name)}」，保留子目录层级。", wraplength=850)
        self.target_label.pack(anchor="w")
        self.source_hint_label = ttk.Label(info,
            text="默认只预览上述文件夹的顶层文件；勾选后可选择子文件夹。预览只读扫描，不移动文件。", wraplength=850)
        self.source_hint_label.pack(anchor="w")
        self.path_label = ttk.Label(info, text=f"下载目录：{self._public(self.settings.downloads_dir)}",
                                    wraplength=850)
        self.path_label.pack(anchor="w")
        self.path_label.configure(text="完整路径：选择整理文件夹后可查看")
        self.plan_label = ttk.Label(info, text="计划：尚未生成", wraplength=850)
        self.plan_label.pack(anchor="w")
        self.hash_label = ttk.Label(info, text="计划指纹：—", wraplength=850)
        self.hash_label.pack(anchor="w")
        self.summary_label = ttk.Label(info, text="方案：—", wraplength=850)
        self.summary_label.pack(anchor="w")
        self.status_label = ttk.Label(info, text=f"状态：{STATE_IDLE}", wraplength=850)
        self.status_label.pack(anchor="w")
        notebook = ttk.Notebook(self.details_window)
        notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.detail_notebook = notebook
        files = ttk.Frame(notebook, padding=8)
        notebook.add(files, text="完整文件明细")
        files.columnconfigure(0, weight=1)
        files.rowconfigure(0, weight=1)
        self.detail_tree = ttk.Treeview(files, columns=columns[:5], show="headings")
        for key, title, width in (("rel", "相对路径", 200), ("source", "完整原位置", 350),
                                  ("dest", "完整目标位置", 400), ("basis", "分类依据", 260),
                                  ("size", "大小（逻辑）", 100)):
            self.detail_tree.heading(key, text=title)
            self.detail_tree.column(key, width=width, minwidth=100, anchor="w", stretch=False)
        self.detail_tree.grid(row=0, column=0, sticky="nsew")
        sx = ttk.Scrollbar(files, orient="horizontal", command=self.detail_tree.xview)
        sx.grid(row=1, column=0, sticky="ew")
        sy = ttk.Scrollbar(files, orient="vertical", command=self.detail_tree.yview)
        sy.grid(row=0, column=1, sticky="ns")
        self.detail_tree.configure(xscrollcommand=sx.set, yscrollcommand=sy.set)
        skipped = ttk.Frame(notebook, padding=8)
        notebook.add(skipped, text="跳过项目与原因")
        self.skip_text = tk.Text(skipped, height=8, wrap="word", state="disabled")
        self.skip_text.pack(fill="both", expand=True)
        history = ttk.Frame(notebook, padding=12)
        notebook.add(history, text="执行与撤销")
        self.result_label = ttk.Label(history, text="上次执行：尚无记录\n撤销结果：尚未撤销",
                                      wraplength=780, justify="left")
        self.result_label.pack(anchor="w", pady=(0, 12))
        self.recover_button = ttk.Button(history, text="恢复/检查", command=self.on_recover)
        self.recover_button.pack(anchor="w")
        ttk.Label(history, text="原分步操作", foreground="#555555").pack(anchor="w", pady=(16, 4))
        self.confirm_button = ttk.Button(history, text="确认方案", command=self.on_confirm, state="disabled")
        self.confirm_button.pack(anchor="w")
        self.execute_button = ttk.Button(history, text="执行移动", command=self.on_execute, state="disabled")
        self.execute_button.pack(anchor="w", pady=4)
        ttk.Label(history, text=FOOTER_WARNING, wraplength=780).pack(anchor="w", pady=12)
        records = ttk.Frame(notebook, padding=8)
        notebook.add(records, text="逐项记录与新文件")
        self.history_text = tk.Text(records, height=10, wrap="word", state="disabled")
        self.history_text.pack(fill="both", expand=True)
        self.discovery_detail = tk.Text(records, height=6, wrap="word", state="disabled")
        self.discovery_detail.pack(fill="x")

        self.settings_window = self._aux_window("整理设置")
        self.settings_window.protocol("WM_DELETE_WINDOW", self.on_toggle_settings)
        self.archive_check = ttk.Checkbutton(self.settings_window,
            text="归档本次整理后变空的文件夹", variable=self.archive_var, command=self.on_toggle_archive)
        self.archive_check.pack(anchor="w", padx=12, pady=(0, 12))
        self.archive_label = ttk.Label(info, text="", wraplength=780)
        self.archive_label.pack(anchor="w")
        directory_tab = ttk.Frame(notebook, padding=8)
        notebook.add(directory_tab, text="条件目录归档")
        self.directory_text = tk.Text(directory_tab, state="disabled", wrap="word")
        self.directory_text.pack(fill="both", expand=True)
        watch = ttk.LabelFrame(self.settings_window, text="发现新文件（仅提示）", padding=12)
        watch.pack(fill="x", padx=12)
        self._watch_enabled_var = tk.BooleanVar(value=False)
        self.watch_check = ttk.Checkbutton(watch, text="自动检查新文件（每 30 秒，仅提示）",
            variable=self._watch_enabled_var, command=self.on_toggle_watch)
        self.watch_check.pack(anchor="w")
        self.watch_now_button = ttk.Button(watch, text="立即检查", command=self.on_check_now)
        self.watch_now_button.pack(anchor="w", pady=6)
        self.watch_label = ttk.Label(watch, text="自动检查：已关闭（不会自动移动或确认）", wraplength=780)
        self.watch_label.pack(anchor="w")
        ttk.Label(self.settings_window, text="类别或范围改变后会重新预览，仍须明确确认才能整理。",
                  wraplength=780).pack(anchor="w", padx=12, pady=12)
        # 无预览时也能选择类别，生成计划时沿用同一失效路径。
        self._render_categories(CATEGORIES, {}, {})

    def _build_footer(self) -> None:
        footer = tk.Frame(self.main_frame, padx=16, pady=10, background="#f7f7f8")
        self.footer_frame = footer
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        # 状态文字仍由 daily_status_label 维护，由顶部结果条展示；底栏只放固定安全提示。
        self.daily_status_label = tk.Label(footer, text="先生成预览，看看哪些文件可以整理。",
                                            background="#f7f7f8", wraplength=210)
        self.footer_hint_label = tk.Label(footer, text=FOOTER_HINT, background="#f7f7f8",
                                          foreground="#6e6e73", anchor="w")
        self.footer_hint_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        buttons = tk.Frame(footer, background="#f7f7f8")
        buttons.grid(row=0, column=1, sticky="e")
        self.undo_button = ttk.Button(buttons, text="撤销上一次", command=self.on_undo, state="disabled",
                                      style="Tool.TButton")
        self.undo_button.pack(side="left", padx=(0, 6))
        self.remaining_button = ttk.Button(buttons, text="归档剩余项目…", command=self.on_remaining_archive,
                                           style="Tool.TButton")
        self.remaining_button.pack(side="left", padx=(0, 6))
        # Both original callbacks retain their own enabled state; only one is shown.
        self.preview_button = ttk.Button(buttons, text="生成预览", command=self.on_preview, default="active",
                                         style="Tool.TButton")
        self.organize_button = ttk.Button(buttons, text="确认整理", command=self.on_organize,
                                          state="disabled", default="active", style="Tool.TButton")
        self.remaining_label = ttk.Label(self.details_window, text="", wraplength=780)
        self.remaining_label.pack(fill="x", padx=12)
        self.watch_results = tk.Frame(self.banner, background="#e8efff")
        self.watch_text = tk.Text(self.watch_results, height=3, wrap="word", state="disabled",
                                   borderwidth=0, highlightthickness=0, font=("", 12))
        self.watch_scroll = ttk.Scrollbar(self.watch_results, orient="vertical", command=self.watch_text.yview)
        self.watch_text.configure(yscrollcommand=self.watch_scroll.set)
        self.watch_scroll.pack(side="right", fill="y")
        self.watch_text.pack(side="left", fill="both", expand=True)
        self.watch_more_button = ttk.Button(self.banner, text="查看更多新文件提示", command=self.on_more_discovery)
        self._preview_problem = ""
        self._state_problem = ""
        self._discovery_problem = ""
        self.root.bind("<Configure>", self._fit_daily_labels, add="+")
        self._build_operations_window()

    def _fit_daily_labels(self, event) -> None:
        if event.widget is self.root:
            width = max(240, event.width - 280)
            self.problem_label.configure(wraplength=width)
            self.banner_title.configure(wraplength=max(160, width - 150))
            self.empty_title.configure(wraplength=min(420, width - 40))
            self.empty_description.configure(wraplength=min(380, width - 40))
            self.daily_status_label.configure(wraplength=max(120, width - 430))
            self.footer_hint_label.configure(wraplength=max(120, width - 430))

    def on_toggle_details(self) -> None:
        if self.details_window.state() == "withdrawn":
            self.details_window.deiconify()
            self.details_button.configure(text="收起详情")
        else:
            self.details_window.withdraw()
            self.details_button.configure(text="查看详情")

    def on_more_discovery(self) -> None:
        if self.details_window.state() == "withdrawn":
            self.on_toggle_details()
        self.detail_notebook.select(3)

    def on_toggle_settings(self) -> None:
        if self.settings_window.state() == "withdrawn":
            self.settings_window.deiconify()
        else:
            self.settings_window.withdraw()

    def on_pick_source(self) -> None:
        if self._operation_busy or self._closing:
            return
        chosen = filedialog.askdirectory(parent=self.root, title='选择整理文件夹', mustexist=True)
        # 预览只读：从选择框选好且没有可恢复的整理记录时，直接生成预览。
        if chosen and self.select_source(chosen) and self.plan is None:
            self.on_preview()

    def select_source(self, path: str) -> bool:
        """文件选择对话框和测试共用的真实准入路径；失败不改变任何当前状态。"""
        if self._operation_busy or self._closing:
            return False
        try:
            settings = selected_settings(self._base_settings, path)
        except (ConfigError, OSError, ValueError) as exc:
            messagebox.showerror(WINDOW_TITLE, self._public(exc))
            return False
        if self._source_selected and settings.downloads_dir == self.settings.downloads_dir:
            return True
        self._invalidate_discovery()
        self._cancel_watch_job()
        self.watcher = NewFileWatcher(clock=self._clock)
        self._watch_enabled_var.set(False)
        self.watch_label.configure(text='自动检查：已关闭（不会自动移动或确认）')
        self._set_watch_text('')
        self.settings = settings
        self._remaining_epoch += 1
        if self._remaining_dialog is not None and self._remaining_dialog.window.winfo_exists():
            self._remaining_dialog.invalidate_context()
        self._chain_result = None
        self._source_selected = True
        self._view_category = None
        self.selected_scope = []
        self.include_subfolders = False
        self.subfolder_var.set(False)
        self.archive_var.set(False)
        self.archive_label.configure(text="")
        self.pick_button.configure(state='disabled')
        self.plan = None
        self.confirmed_hash = None
        self._preview_ready = False
        for tree in (self.tree, self.detail_tree):
            for item in tree.get_children():
                tree.delete(item)
        for text in (self.skip_text, self.history_text):
            text.configure(state='normal'); text.delete('1.0', 'end'); text.configure(state='disabled')
        self.remaining_label.configure(text='')
        self._render_categories(self._selected_categories(), {}, {})
        self.result_label.configure(text='上次执行：—')
        self._preview_problem = self._state_problem = self._discovery_problem = ''
        name = self._public(os.path.basename(settings.downloads_dir) or settings.downloads_dir)
        mode = '人工测试目录' if settings.sandboxed else '已选择目录'
        self.source_label.configure(text=f'整理位置：{name}（{mode}）')
        self.path_label.configure(text=f'下载目录：{self._public(settings.downloads_dir)}')
        self.scope_label.configure(text=self.scope_summary())
        self.plan_label.configure(text='计划：尚未生成')
        self.hash_label.configure(text='计划指纹：—')
        self.summary_label.configure(text='方案：—')
        self._set_status(STATE_IDLE)
        self._load_latest_plan_if_any()
        self._preview_ready = False  # 选择/切换来源不复用历史预览授权。
        self._refresh_plan_buttons()
        return True

    def _render_remaining(self, plan) -> None:
        names = {'文件夹': '未选择的文件夹', '疑似敏感项': '疑似私人项目',
                 '疑似敏感目录（整体跳过）': '疑似私人目录',
                 '空文件夹（无文件，整体跳过）': '空文件夹', '包目录（整体跳过）': '应用／包目录',
                 '未完成下载或临时项': '未完成下载', '代码项目目录（整体跳过）': '受保护的成套项目'}
        parts = []
        for label, value in plan.get('excluded', {}).items():
            if label == '跳过原因':
                continue
            count = value.get('count', 0) if isinstance(value, dict) else len(value)
            if count:
                parts.append(f'{names.get(label, label)} {count}')
        counts = plan.get('counts', {})
        unselected = counts.get('included', 0) - counts.get('selected', 0)
        if unselected > 0:
            parts.append(f'未选类别 {unselected}')
        self.remaining_label.configure(text=('本次预览未整理项目：' + '；'.join(parts) + '。查看详情了解原因。') if parts else '')

    def _build_operations_window(self) -> None:
        self.operations_window = self._aux_window('本次操作记录')
        self.operations_window.protocol('WM_DELETE_WINDOW', self.on_toggle_operations)
        self.operation_hint_label = ttk.Label(self.operations_window, text='只记录本次打开期间的工具操作，不是文件内容快照。\n'
                  '退回时逆序撤销后续步骤；一批未完整还原就停止，不覆盖外部修改。',
                  wraplength=700)
        self.operation_hint_label.pack(fill='x', padx=12, pady=12)
        actions = ttk.Frame(self.operations_window, padding=12)
        actions.pack(side='bottom', fill='x')
        self.return_button = ttk.Button(actions, text='退回到选中步骤', command=self.on_return_selected)
        self.return_button.pack(side='left')
        self.return_zero_button = ttk.Button(actions, text='退回本次开始前（V0）', command=lambda: self.on_return_to(0))
        self.return_zero_button.pack(side='left', padx=8)
        self.step_recover_button = ttk.Button(actions, text='检查选中批次', command=self.on_recover_step)
        self.step_recover_button.pack(side='left')
        self.operation_detail_label = ttk.Label(self.operations_window, padding=12, wraplength=840,
            text='选择一条记录可查看完整来源和本批结果。')
        self.operation_detail_label.pack(side='bottom', fill='x')
        self.operations_window.bind('<Configure>', self._fit_operation_labels)
        panel = ttk.Frame(self.operations_window, padding=12)
        panel.pack(fill='both', expand=True)
        self.operations_tree = ttk.Treeview(panel, columns=('source', 'count', 'state', 'parent'), show='tree headings')
        self.operations_tree.heading('#0', text='版本 / 计划')
        for col, label in [('source', '所属文件夹'), ('count', '移动 / 待判定'), ('state', '状态'), ('parent', '来自版本')]:
            self.operations_tree.heading(col, text=label)
            self.operations_tree.column(col, width=140, minwidth=80)
        bar = ttk.Scrollbar(panel, orient='vertical', command=self.operations_tree.yview)
        horizontal = ttk.Scrollbar(panel, orient='horizontal', command=self.operations_tree.xview)
        panel.columnconfigure(0, weight=1); panel.rowconfigure(0, weight=1)
        bar.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        self.operations_tree.configure(yscrollcommand=bar.set, xscrollcommand=horizontal.set)
        self.operations_tree.grid(row=0, column=0, sticky='nsew')
        self.operations_tree.bind('<<TreeviewSelect>>', self.on_describe_operation)
        self._render_operations()

    def _fit_operation_labels(self, event=None) -> None:
        if self._closing:
            return
        width = max(250, self.operations_window.winfo_width() - 48)
        self.operation_hint_label.configure(wraplength=width)
        self.operation_detail_label.configure(wraplength=width)

    def _render_operations(self) -> None:
        selected = self.operations_tree.selection()
        for item in self.operations_tree.get_children():
            self.operations_tree.delete(item)
        self.operations_tree.insert('', 'end', iid='0', text='V0：本次开始前', values=('', '', '起点', ''))
        for s in self.session.steps:
            source = redact_private_text(s.settings.downloads_dir, roots=('/private/tmp', '/private/var'))
            # 列表保留完整来源按需核对，名称另经独立脱敏，不让可信锚点豁免文件夹名。
            source = self._public(os.path.basename(s.settings.downloads_dir)) + ' · ' + source
            state = {'active': '尚未撤销', 'undone': '已撤销的分支', 'needs_review': '受阻／需检查'}[s.status]
            if s.status == 'active' and s.checked:
                state = '已检查，可尝试撤销'
            if s.status == 'active' and (s.skipped or s.failed or s.not_attempted):
                state = f'部分整理：跳过 {s.skipped}、失败 {s.failed}、未处理 {s.not_attempted}'
            self.operations_tree.insert('', 'end', iid=str(s.version), text=f'V{s.version} · {s.plan_id}',
                values=(source, f'{s.moved} / {s.uncertain}' + (f'；目录 {s.archived_dirs}' if s.archived_dirs or s.uncertain_dirs else ''), state, f'V{s.parent}'))
        for item in selected:
            if self.operations_tree.exists(item):
                self.operations_tree.selection_set(item)
        for button in (self.return_button, self.return_zero_button):
            button.configure(state='normal' if self.session.active and not self._operation_busy else 'disabled')
        self.on_describe_operation()

    def on_describe_operation(self, event=None) -> None:
        if self._closing:
            return
        selected = self.operations_tree.selection()
        step = next((s for s in self.session.steps if selected and str(s.version) == selected[0]), None)
        if step:
            source = redact_private_text(step.settings.downloads_dir, roots=('/private/tmp', '/private/var'))
            text = (f'V{step.version} · {step.plan_id}\n所属文件夹：{source}\n'
                    f'上次整理：移动 {step.moved}、跳过 {step.skipped}、失败 {step.failed}、'
                    f'未处理 {step.not_attempted}；待判定 {step.uncertain} 项。'
                    + (f'\n目录归档 {step.archived_dirs}、待判定 {step.uncertain_dirs}。' if step.archived_dirs or step.uncertain_dirs else ''))
        elif selected:
            text = 'V0：退回本次工具开始整理前；不删除外部新增文件，不承诺还原会话起点之外的旧历史。'
        else:
            text = '选择一条记录可查看完整来源和本批结果。'
        self.operation_detail_label.configure(text=text)

    def on_toggle_operations(self) -> None:
        self._render_operations()
        if self.operations_window.state() == 'withdrawn':
            self.operations_window.deiconify()
        else:
            self.operations_window.withdraw()

    def on_return_selected(self) -> None:
        selection = self.operations_tree.selection()
        if selection:
            self.on_return_to(int(selection[0]))

    def on_recover_step(self) -> None:
        if self._operation_busy:
            return
        selection = self.operations_tree.selection()
        step = next((s for s in self.session.steps if selection and str(s.version) == selection[0]), None)
        if step is None:
            return
        self._operation_busy = True
        self._refresh_plan_buttons()
        try:
            result = recover_execution(step.settings, step.plan_id, kind='gui')
            self.session.checked_batch(step.settings, step.plan_id)
            messagebox.showinfo(WINDOW_TITLE, f'V{step.version} 检查结果：{result["phase"]}')
        except (PlanError, ExecutionRefused, LockBusy, OSError) as exc:
            messagebox.showwarning(WINDOW_TITLE, self._public(exc))
        finally:
            self._operation_busy = False
            self._refresh_plan_buttons()

    def on_return_to(self, version: int, simple: bool = False) -> None:
        """simple=True 来自「撤销上一次」：短确认框、成功后不弹提示；操作记录窗口保留完整明细。"""
        if self._operation_busy:
            return
        try:
            batches = self.session.batches_after(version)
        except ValueError as exc:
            messagebox.showwarning(WINDOW_TITLE, str(exc))
            return
        if not batches:
            return
        summary = '\n'.join(f'V{s.version} · {s.plan_id}\n  来源：{redact_private_text(s.settings.downloads_dir, roots=("/private/tmp", "/private/var"))}\n'
                            f'  已移动 {s.moved} 个文件、已归档 {s.archived_dirs} 个目录，待判定 {s.uncertain} 项（其中目录 {s.uncertain_dirs} 项）' for s in batches)
        report = None
        self._operation_busy = True
        self._invalidate_discovery()
        self._refresh_plan_buttons()
        try:
            moved = sum(s.moved for s in batches)
            pending = sum(s.uncertain for s in batches)
            if simple:
                asked = messagebox.askokcancel('确认撤销', '撤销上一次整理？', detail=(
                    f'{moved} 个文件' + (f'、{sum(s.archived_dirs for s in batches)} 个原目录' if any(s.archived_dirs for s in batches) else '')
                    + '会放回原来的位置。' + (f'另有 {pending} 项需要先核对。' if pending else '') +
                    '\n之后新放进来的文件不受影响；如果某个文件已被改动，会停下来提示你。'))
            else:
                asked = messagebox.askokcancel('确认退回操作', f'将按以下顺序撤销，{"把文件放回本次整理前的位置" if version == 0 else f"退回 V{version}"}：\n\n{summary}\n\n'
                    f'预计最多还原 {sum(s.moved for s in batches)} 个已知移动文件；另有 {sum(s.uncertain for s in batches)} 项待判定。\n'
                    f'原目录 {sum(s.archived_dirs for s in batches)} 个；目录待判定 {sum(s.uncertain_dirs for s in batches)} 项。\n'
                    '不会删除外部新增文件或覆盖外部修改。某批未完整还原就停止。\n'+FOOTER_WARNING)
            if not asked:
                return
            report = self.session.return_to(version, undo_plan)
            self._chain_result = report
            self._refresh_plan_buttons()
            if report.reached:
                self._chain_problem = ""
                dirs = f'、{report.restored_dirs} 个原目录' if report.restored_dirs else ''
                if not simple:
                    messagebox.showinfo(WINDOW_TITLE, f'已还原 {report.restored} 个文件{dirs}到原位置。'
                                        + (f'（已退回 V{version}）' if version else ''))
            else:
                stopped = next(s for s in self.session.steps if s.version == report.stopped_at)
                source = redact_private_text(stopped.settings.downloads_dir,
                                             roots=("/private/tmp", "/private/var"))
                self._chain_problem = (f'本次会话 V{report.stopped_at} 撤销受阻（原来源：{source}）。'
                    + self._public(f'未退回 V{version}。'
                    f'本次确定已还原 {report.restored} 个文件、{report.restored_dirs} 个原目录；更早步骤未撤销。{report.detail}'))
                self._show_daily_problems()
                messagebox.showwarning(WINDOW_TITLE, self._chain_problem)
        finally:
            self._operation_busy = False
            self._refresh_plan_buttons()
        if report and report.exception:
            self.status_label.configure(text='状态：本次撤销失败（未确认全部还原）')
            self.result_label.configure(text=str(self.result_label['text']) + '（本次撤销失败）')

    def _render_categories(self, selected, counts, sizes) -> None:
        for child in self.overview_frame.winfo_children():
            child.destroy()
        self._selection_vars = {c: tk.BooleanVar(value=c in selected) for c in CATEGORIES}
        self._category_counts = dict(counts)
        self._category_widgets = {}
        colors = {"图片截图": "#0f9d76", "文档资料": "#2563eb", "安装包": "#c2410c",
                  "压缩包": "#a16207", "音视频": "#7c3aed", "代码与数据": "#0e7490", "待确认": "#6b7280"}
        for category in (None, *colors):
            row = tk.Frame(self.overview_frame, background="#f1f2f5", padx=6, pady=5)
            row.pack(fill="x", pady=1)
            check = None
            if category:
                check = ttk.Checkbutton(row, variable=self._selection_vars[category],
                                        command=self.on_toggle_category, style="Side.TCheckbutton")
                check.pack(side="left")
            dot = tk.Label(row, text="●", fg=colors.get(category, "#1c1d21"), bg="#f1f2f5", font=("", 10))
            dot.pack(side="left", padx=(0, 5))
            label = tk.Label(row, text=category or "全部", bg="#f1f2f5", anchor="w", font=("", 12))
            label.pack(side="left", fill="x", expand=True)
            count = tk.Label(row, text="", bg="#f1f2f5", fg="#6e6e73", font=("", 12))
            count.pack(side="right")
            for widget in (row, dot, label, count):
                widget.bind("<Button-1>", lambda event, c=category: self.on_filter_category(c))
            self._category_widgets[category] = (row, check, dot, label, count)

    def on_filter_category(self, category) -> None:
        """Viewing one category never changes selection, confirmation or the plan."""
        if self._closing or self._operation_busy:
            return
        self._view_category = category
        self._sync_daily_view(self._history_view, self._plan_matches_current())

    def on_show_skips(self) -> None:
        if self.details_window.state() == "withdrawn":
            self.on_toggle_details()
        self.detail_notebook.select(1)

    def _sync_category_rows(self, history, undone) -> None:
        consumed = history.get("consumed", False) and not undone
        self.category_title.configure(text="这次整理的结果" if consumed else "本次整理的类别")
        operations = (self.plan or {}).get("operations", [])
        execution = history.get("execution", {})
        for category, (row, check, dot, label, count) in self._category_widgets.items():
            chosen = category == self._view_category
            color = "#e4e4ea" if chosen else "#f1f2f5"
            row.configure(background=color)
            for widget in (dot, label, count):
                widget.configure(background=color)
            if check:
                check.configure(style="SideSel.TCheckbutton" if chosen else "Side.TCheckbutton")
            if check:
                if consumed:
                    check.pack_forget()
                else:
                    check.pack(side="left", before=dot)
                    check.configure(state="normal" if self._source_selected and not self._operation_busy else "disabled")
            selected = category is None or self._selection_vars[category].get()
            label.configure(foreground="#1c1d21" if self._source_selected and (selected or consumed) else "#9a9da5")
            if consumed:
                ops = [op for op in operations if category is None or op.get("category") == category]
                outcomes = [(execution.get(op["op_id"], {}).get("result") or {}).get("outcome") for op in ops]
                moved, skipped = outcomes.count("moved"), outcomes.count("skipped")
                text = str(moved) + (f" +{skipped}" if skipped else "")
                if len(outcomes) > moved + skipped:
                    text += f" · 待查 {len(outcomes) - moved - skipped}"
                count.configure(text=text, foreground="#9a5b00" if skipped else "#5d6068")
            elif not self.plan or undone:
                count.configure(text="")
            elif category is None:
                count.configure(text=f"{len(operations)} / {sum(self._category_counts.values())}", foreground="#5d6068")
            else:
                count.configure(text=str(self._category_counts.get(category, 0)), foreground="#5d6068")
        excluded = (self.plan or {}).get("excluded", {})
        skipped = sum(value.get("count", 0) if isinstance(value, dict) else len(value)
                      for key, value in excluded.items() if key not in {"跳过原因", "未归档原因数量"})
        if self.plan and skipped:
            done = consumed and not undone
            self.skip_button.configure(text=f"{skipped} 个{'已跳过' if done else '会跳过'} · 看原因", state="normal")
            self.skip_button.grid(row=5, column=0, sticky="ew", pady=8)
        else:
            self.skip_button.configure(text="跳过 0 项 · 看原因", state="disabled")
            self.skip_button.grid_remove()

    def _draw_empty_icon(self, kind: str) -> None:
        """空状态的线框图标：folder（选文件夹／生成预览）或 undo（撤销之后）。"""
        canvas = self.empty_icon
        canvas.delete("all")
        line = dict(fill="#a1a1a6", width=2, capstyle="round", joinstyle="round")
        if kind == "undo":
            canvas.create_line(18, 8, 8, 18, 18, 28, **line)
            canvas.create_line(8, 18, 32, 18, **line)
            canvas.create_arc(20, 18, 44, 40, start=270, extent=180, style="arc", outline="#a1a1a6", width=2)
            canvas.create_line(32, 40, 26, 40, **line)
        else:
            canvas.create_line(4, 10, 4, 38, 48, 38, 48, 14, 24, 14, 20, 8, 6, 8, 4, 10, **line)

    def _sync_daily_view(self, history, current_plan) -> None:
        if not hasattr(self, "preview_button") or self._closing:
            return
        self._history_view = history
        status = str(self.status_label["text"])
        undone = (status.startswith("状态：" + STATE_UNDONE)
                  or bool(self._chain_result and self._chain_result.reached))
        consumed = history.get("consumed", False)
        name = self._public(os.path.basename(self.settings.downloads_dir)) if self._source_selected else "尚未选择文件夹"
        self.folder_name_label.configure(text=name[:32] + ("…" if len(name) > 32 else ""))
        self.subfolder_check.configure(state="normal" if self._source_selected and not self._operation_busy else "disabled")
        if self._source_selected:
            self.source_button.pack(anchor="w")
        else:
            self.source_button.pack_forget()
        show_empty = not self._source_selected or self.plan is None or undone
        self._sync_category_rows(history, undone)
        self._render_file_view(history)
        self.preview_button.pack_forget()
        self.organize_button.pack_forget()
        if show_empty:
            self.review_frame.grid_remove()
            self.empty_frame.grid(row=0, column=0, sticky="nsew")
            if not self._source_selected:
                title = "先选择要整理的文件夹"
                explanation = "选好后会先生成只读预览，告诉你每个文件会被放到哪里。确认之前不会移动任何文件。"
                text, command = "选择文件夹…", self.on_pick_source
            else:
                title = "需要的话，重新生成预览" if undone else "先生成预览，看看哪些文件可以整理"
                explanation = (f"撤销后可调整类别，再为 {name} 生成一次预览。" if undone else
                               "预览只读扫描，按文件名和类型分好类，不移动任何东西。")
                text, command = "生成预览", self.on_preview
            self._draw_empty_icon("undo" if undone else "folder")
            self.empty_title.configure(text=title)
            self.empty_description.configure(text=explanation)
            self.empty_action.configure(text=text, command=command, state="disabled" if self._operation_busy else "normal")
            self.primary_button = self.empty_action
        else:
            self.empty_frame.grid_remove()
            self.review_frame.grid(row=0, column=0, sticky="nsew")
            if current_plan:
                self.organize_button.configure(text=f"确认整理 {len(self.plan.get('operations', []))} 个文件")
                self.organize_button.pack(side="left")
                self.primary_button = self.organize_button
            else:
                self.preview_button.configure(text="重新生成预览")
                self.preview_button.pack(side="left")
                self.primary_button = self.preview_button
        if self._source_selected:
            self.footer_frame.grid()
        else:
            self.footer_frame.grid_remove()
        if show_empty:
            self.toolbar_title.configure(text=name if self._source_selected else "整理文件")
        else:
            count = len(self.tree.get_children())
            self.toolbar_title.configure(text=f"{self._view_category} · {count} 个" if self._view_category else "全部文件")
        self.mode_label.configure(text="预览 · 只读" if current_plan else ("整理结果" if consumed and not undone else "未预览"),
                                  background="#ececf0")
        self._sync_banner()

    def _sync_banner(self) -> None:
        if not hasattr(self, "watch_text") or self._closing:
            return
        history = self._history_view
        status = str(self.status_label["text"])
        undone = status.startswith("状态：" + STATE_UNDONE) or bool(self._chain_result and self._chain_result.reached)
        executed = history.get("phase") == "executed" and status in {f"状态：{STATE_EXECUTED}", "状态：已执行（无可撤销项）"}
        watching = bool(self.watch_results.winfo_manager())
        problem = str(self.problem_label["text"])
        self.finder_button.pack_forget()
        if executed:
            self.finder_button.pack(side="right", padx=(8, 0))
            self.finder_button.configure(state="normal" if self._target_can_open() and not self._operation_busy else "disabled")
        if not (history.get("consumed") or problem or watching or self._chain_result):
            self.banner.grid_remove()
            return
        counts = history.get("state", {}).get("counts") or {}
        bad = bool(self._preview_problem or self._discovery_problem or getattr(self, '_chain_problem', '')
                   or history.get('error') or (history.get('consumed') and not (executed or undone))
                   or (problem and not (executed or undone))
                   or (executed and any(counts.get(k, 0) for k in ('failed', 'not_attempted'))))
        # 三种含义：琥珀＝有问题／跳过，灰＝普通提示（撤销、新文件），绿＝整理完成。
        bg, fg = ("#fff3dc", "#9a5b00") if bad else (("#f1f1f4", "#3a3a3c") if undone or watching else ("#e5f4ec", "#16794c"))
        self.banner.configure(background=bg)
        style = ttk.Style(self.root)
        style.configure("Banner.TFrame", background=bg)
        style.configure("Banner.TButton", background=bg)
        self.banner_heading.configure(background=bg)
        self.watch_results.configure(background=bg)
        title = "发现新文件 · 请重新生成预览" if watching and not bad else str(self.daily_status_label["text"])
        self.banner_title.configure(text=title, background=bg, foreground=fg)
        self.problem_label.configure(background=bg, foreground=fg)
        self.watch_text.configure(background=bg, foreground=fg)
        self.banner.grid(row=1, column=0, sticky="ew")

    def _target_can_open(self) -> bool:
        if not self._source_selected or self._history_view.get("phase") != "executed":
            return False
        try:
            return stat.S_ISDIR(os.lstat(self.settings.target_root).st_mode)
        except OSError:
            return False

    def on_open_target(self) -> None:
        if self._closing or self._operation_busy or not self._target_can_open():
            return
        try:
            subprocess.Popen(["/usr/bin/open", self.settings.target_root])
        except OSError as exc:
            messagebox.showerror(WINDOW_TITLE, self._public(exc))

    def _render_file_view(self, history) -> None:
        self._hide_tooltip()
        selected = self.tree.selection()
        y = self.tree.yview()
        for row in self.tree.get_children():
            self.tree.delete(row)
        plan = public_plan_view(self.plan, self.settings) if self.plan else {}
        operations = plan.get("operations", [])
        if plan.get("plan_type") == "remaining_archive":
            operations = operations + plan.get("directory_operations", [])
        consumed = history.get("consumed", False)
        columns = ["rel"] + (["format"] if self._view_category == "文档资料" else []) + ["size", "folder"]
        if consumed:
            columns.append("result")
        self.tree.configure(displaycolumns=columns)
        for op in operations:
            if self._view_category and op.get("category") != self._view_category:
                continue
            event = (history.get("execution", {}).get(op["op_id"], {}).get("result") or {})
            outcome = event.get("outcome")
            result = {"moved": "已移动", "archived": "已归档", "skipped": "跳过", "failed": "失败"}.get(outcome, "待检查" if consumed else "")
            if outcome == "skipped" and event.get("reason_code") == "DEST_EXISTS":
                result = "跳过 · 同名文件已存在，未覆盖"
            elif consumed and outcome not in {"moved", "archived"} and event.get("reason"):
                result += " · " + self._public(event["reason"])
            folder = os.path.dirname(os.path.relpath(op["dest"], self.settings.downloads_dir))
            name = op.get("rel_path") or os.path.basename(op["source"])
            values = (name, op["source"], op["dest"], op.get("classification_basis", "原本空目录"),
                      human_size(int(op["identity"]["size_bytes"])) if "size_bytes" in op.get("identity", {}) else "—",
                      folder, document_subcategory(normalize_extension(name)) or "—", result)
            tags = ("skipped",) if consumed and outcome not in {"moved", "archived"} else (("even",) if len(self.tree.get_children()) % 2 else ())
            self.tree.insert("", "end", iid=op["op_id"], values=values, tags=tags)
        for row in selected:
            if self.tree.exists(row):
                self.tree.selection_add(row)
        if y:
            self.tree.yview_moveto(y[0])
        self._fit_file_columns()

    def _fit_file_columns(self, event=None) -> None:
        width = max(300, self.tree.winfo_width() - 4)
        columns = tuple(self.tree["displaycolumns"])
        weights = {"rel": 40, "size": 12, "folder": 35, "format": 13, "result": 30}
        total = sum(weights[c] for c in columns)
        for col in columns:
            self.tree.column(col, width=int(width * weights[col] / total), minwidth=30, stretch=False)

    def _hide_tooltip(self, event=None) -> None:
        if self._tooltip_job is not None:
            self.root.after_cancel(self._tooltip_job)
            self._tooltip_job = None
        if self._tooltip is not None:
            self._tooltip.destroy()
            self._tooltip = None

    def _file_hover(self, event) -> None:
        self._hide_tooltip()
        row = self.tree.identify_row(event.y)
        if not row:
            return
        values = self.tree.item(row, "values")
        def show():
            self._tooltip_job = None
            if self._closing or not self.tree.exists(row):
                return
            tip = self._tooltip = tk.Toplevel(self.root)
            tip.overrideredirect(True)
            tip.geometry(f"+{min(event.x_root + 12, self.root.winfo_screenwidth() - 500)}+{min(event.y_root + 18, self.root.winfo_screenheight() - 170)}")
            text = values[0] + "\n整理到：" + values[5] + ("\n" + values[7] if len(values) > 7 and values[7] else "")
            ttk.Label(tip, text=text, padding=10, wraplength=470, background="#fff9dc").pack()
        self._tooltip_job = self.root.after(500, show)

    # --- 状态 ---------------------------------------------------------------
    def _set_status(self, state: str) -> None:
        self.status_label.configure(text=f"状态：{state}")
        self._state_problem = ""
        if state == STATE_IDLE:
            text = "先生成预览，看看哪些文件可以整理。"
        elif state == STATE_ERROR:
            text = "操作失败 · 请查看提示并重新生成预览"
            self._state_problem = "当前方案不能继续整理，请查看详情；处理问题后重新生成预览。"
        elif state in {STATE_PREVIEWED, STATE_CONFIRMED}:
            n = len((self.plan or {}).get("operations", []))
            text = f"待整理 {n} 个文件 · 请点击确认整理" if n else "没有待整理文件 · 可调整范围或设置"
        else:
            text = state
            if any(word in state for word in ("检查", "失败", "出错", "部分", "未完成")):
                self._state_problem = "操作尚未全部完成，请查看详情；必要时使用恢复/检查。"
        self.daily_status_label.configure(text=text)
        self._show_daily_problems()
        if hasattr(self, "empty_action"):
            self._sync_daily_view(self._history_view, self._plan_matches_current())

    def _selected_categories(self) -> list[str]:
        chosen = [c for c in CATEGORIES if self._selection_vars.get(c) and self._selection_vars[c].get()]
        return chosen or []

    def on_toggle_category(self) -> None:
        if self._operation_busy:
            for c, v in self._selection_vars.items():
                v.set(c in (self.plan or {}).get("category_selection", CATEGORIES))
            return
        """勾选变化 → 确认失效。"""
        if self.plan is not None:
            self.confirmed_hash = None
            self.on_preview()
            self.hash_label.configure(text=f"计划指纹：{(self.plan or {}).get('plan_hash', '—')}（类别已变化，需重新确认）")
        self._refresh_plan_buttons()

    def _plan_scope_matches_current(self) -> bool:
        if self.plan is None or "selected_scope" not in self.plan or self.archive_var.get() != self.plan.get("archive", {}).get("enabled", False):
            return False
        if bool(self.subfolder_var.get()) != self.include_subfolders:
            return False
        recursive = bool(self.include_subfolders and self.selected_scope)
        try:
            roots = normalize_scope(self.selected_scope if recursive else [])
            return (
                self.plan.get("recursive") is recursive
                and self.plan.get("selected_scope") == roots
                and self.plan.get("downloads_dir") == normalize_path(self.settings.downloads_dir)
                and self.plan.get("target_root") == normalize_path(self.settings.target_root)
            )
        except (OSError, ValueError):
            return False

    def on_toggle_archive(self) -> None:
        if self._operation_busy:
            self.archive_var.set((self.plan or {}).get("archive", {}).get("enabled", False))
            return
        self._invalidate_preview()
        self.archive_label.configure(text="归档设置已变化，请重新预览" if self.archive_var.get() else "")
        self._refresh_plan_buttons()

    def _plan_matches_current(self) -> bool:
        return bool(
            self._source_selected and self._preview_ready
            and self.plan and self.plan.get('plan_type') != 'remaining_archive'
            and not self._read_plan_history()["consumed"]
            and self._policy_allows_move()
            and self._plan_scope_matches_current()
            and self._selected_categories() == self.plan.get("category_selection")
            and self.archive_var.get() == self.plan.get("archive", {}).get("enabled", False)
        )

    def _policy_allows_move(self) -> bool:
        if self.plan is None:
            return False
        try:
            assert_new_move_admissible(self.settings, self.plan)
        except PlanError:
            return False
        return True

    def _public(self, value: Any) -> str:
        return redact_private_text(str(value), roots=(self.settings.downloads_dir,
                                                     self.settings.target_root,
                                                     self.settings.state_dir))

    def _invalidate_preview(self) -> None:
        self._remaining_epoch += 1
        if self._remaining_dialog is not None and self._remaining_dialog.window.winfo_exists():
            self._remaining_dialog.invalidate_context()
        self._preview_ready = False
        self.confirmed_hash = None
        self._refresh_plan_buttons()

    def _read_plan_history(self, candidate=None) -> dict[str, Any]:
        """只读持久化记录；不能把旧确认指纹当成再次执行授权。"""
        plan = self.plan if candidate is None else candidate
        if plan is None:
            return {"consumed": False}
        plan_id = str(plan["plan_id"])
        state = read_plan_state(self.settings, plan_id)
        snapshot = read_snapshot(self.settings, plan_id)
        error = False
        try:
            if plan.get("schema_version") in (3, 4):
                from .directory_archive import read_folds
                execution,undo=read_folds(self.settings,plan)
            else:
                execution, undo = fold_verified_journals(self.settings.state_dir, plan_id)
        except (JournalCorrupted, JournalNeedsRepair, PlanError, KeyError, TypeError, ValueError, OSError):
            execution, undo, error = {}, {}, True
        phase = str(state.get("phase") or "prepared")
        consumed = bool(error or phase != "prepared" or state.get("counts") is not None
                        or execution or undo or (snapshot and any(
                            e.get("undo_state") != "skipped_not_moved"
                            for e in snapshot.get("entries", []))))
        return dict(state=state, snapshot=snapshot, phase=phase, execution=execution,
                    undo=undo, error=error, consumed=consumed)

    def _render_plan_history(self, history: dict[str, Any]) -> None:
        """上次执行统计与撤销结果分别展示，完成状态须有一致的持久化证据。"""
        state, phase = history["state"], history["phase"]
        execution = history["execution"]
        counts = state.get("counts")
        if isinstance(counts, dict):
            executed = (f"上次执行：成功 {counts.get('moved', 0)} · 跳过 {counts.get('skipped', 0)} · "
                        f"失败 {counts.get('failed', 0)} · 未处理 {counts.get('not_attempted', 0)}")
        elif execution:
            outcomes = [(slot.get("result") or {}).get("outcome") for slot in execution.values() if (slot.get("intent") or {}).get("op_kind","file")=="file"]
            executed = (f"上次执行：成功 {outcomes.count('moved')} · 跳过 {outcomes.count('skipped')} · "
                        f"失败 {outcomes.count('failed')} · 未处理 "
                        f"{len(self.plan.get('operations', [])) - len(outcomes)} · "
                        f"待检查 {sum(o not in {'moved', 'skipped', 'failed'} for o in outcomes)}")
        else:
            executed = "上次执行：记录不可用，请恢复/检查"
        snapshot = history["snapshot"] or {}
        entries = snapshot.get("entries", [])
        directory_entries=snapshot.get("directory_entries",[])
        if self.plan.get('archive',{}).get('enabled'):
            ds=[e.get("undo_state") for e in directory_entries]
            directory_slots=[execution.get(op['op_id'],{}) for op in self.plan.get('directory_operations',[])]
            directory_results=[(v.get('result') or {}).get('outcome') for v in directory_slots]
            executed += (f"\n目录归档：成功 {directory_results.count('archived')} · 未满足 {directory_results.count('skipped')} · "
                         f"失败/待检查 {sum(v in {'failed','unknown_needs_review'} for v in directory_results)} · "
                         f"原目录还原 {ds.count('restored')}、待检查 {ds.count('unknown_needs_review')}")
        states = [e.get("undo_state") for e in entries]
        restored = states.count("restored")
        skipped = states.count("skipped_not_moved")
        pending = sum(s in {"pending", "attempting"} for s in states)
        blocked = sum(str(s).startswith("blocked_") for s in states)
        unknown = len(states) - restored - skipped - pending - blocked
        undo_text = (f"撤销结果：已还原 {restored} · 被阻止 {blocked} · "
                     f"无需还原 {skipped} · 待处理 {pending} · 不可判 {unknown}")
        relevant = len(states) - skipped
        complete = bool(phase == "undone" and snapshot.get("undo_status") == "completed"
                        and relevant > 0 and restored == relevant and not history["error"]
                        and all(e.get("undo_state") in {"restored","skipped_not_moved"} for e in directory_entries))
        remaining_archive = self.plan.get('plan_type') == 'remaining_archive'
        directory_states = [e.get('undo_state') for e in directory_entries]
        if remaining_archive:
            complete = bool(phase == 'undone' and snapshot.get('undo_status') == 'completed'
                and not history['error'] and any(s != 'skipped_not_moved' for s in states + directory_states)
                and all(s in {'restored', 'skipped_not_moved'} for s in states + directory_states))
            unknown += sum(s not in {'restored', 'skipped_not_moved', 'pending', 'attempting'}
                           and not str(s).startswith('blocked_') for s in directory_states)
            pending += sum(s in {'pending', 'attempting'} for s in directory_states)
        if history["error"] or unknown or phase in {
            "corrupted", "aborted_log_failure", "unknown_needs_review", "needs_review", "aborted_global"
        }:
            status = STATE_NEEDS_REVIEW
        elif complete:
            status = (f"{STATE_UNDONE}（已还原 {restored} 个文件、{directory_states.count('restored')} 个目录）"
                      if remaining_archive else f"{STATE_UNDONE}（已还原 {restored} 项）")
        elif not snapshot or phase == "undone":
            status = "需检查（撤销记录不可用或不一致）"
        elif phase == "undoing":
            status = f"撤销进行中（已还原 {restored} 项）"
        elif history["undo"] or phase == "partial_undo":
            status = f"{'部分撤销' if restored else '撤销未完成'}（已还原 {restored} 项）"
        elif phase in {"locked", "executing"}:
            status = "执行进行中"
        else:
            status = STATE_EXECUTED if pending else "已执行（无可撤销项）"
        if not snapshot:
            undo_text = "撤销结果：尚无快照，请恢复/检查"
        if history["error"]:
            undo_text += "（日志需检查，以上仅为已保存快照）"
        self.result_label.configure(text=f"{executed}\n{undo_text}")
        lines = [f"计划：{self.plan['plan_id']}", executed, undo_text]
        for kind, folded in (("整理", execution), ("撤销", history["undo"])):
            for op in self.plan.get("operations", []) + self.plan.get("directory_operations", []):
                slot = folded.get(op["op_id"], {})
                event = slot.get("result" if kind == "整理" else "undo_result") or slot.get("undo_skip")
                if event:
                    lines.append(f"{kind} · {op.get('rel_path', op['source'])} · {event.get('outcome', '')} · {event.get('reason', '')}")
        self.history_text.configure(state="normal")
        self.history_text.delete("1.0", "end")
        self.history_text.insert("1.0", self._public("\n".join(lines)))
        self.history_text.configure(state="disabled")
        self._set_status(status)

    def _refresh_plan_buttons(self) -> None:
        has_plan = self.plan is not None
        history = self._read_plan_history()
        if history["consumed"]:
            self._preview_ready = False
            self.confirmed_hash = None
            self._render_plan_history(history)
        current_plan = self._plan_matches_current()
        can_execute = bool(current_plan and self.confirmed_hash == (self.plan or {}).get("plan_hash"))
        self.confirm_button.configure(
            state="normal" if (current_plan and not can_execute) else "disabled"
        )
        self.execute_button.configure(state="normal" if can_execute else "disabled")
        undo_enabled = "disabled"
        if has_plan:
            snapshot = history["snapshot"]
            if not history["error"] and history["phase"] not in {
                "undoing", "corrupted", "needs_review", "unknown_needs_review",
                "aborted_log_failure", "aborted_global"
            } and snapshot and any(
                e.get("undo_state") in {"pending", "attempting"} for e in snapshot.get("entries", [])
                    + snapshot.get("directory_entries", [])
            ):
                undo_enabled = "normal"
        self.undo_button.configure(state=undo_enabled)
        self.organize_button.configure(state="normal" if current_plan and self.plan.get("operations") else "disabled")
        self.recover_button.configure(state="normal" if has_plan else "disabled")
        if history["consumed"]:
            counts = history["state"].get("counts")
            if not isinstance(counts, dict):
                outcomes = [(slot.get("result") or {}).get("outcome") for slot in history["execution"].values()]
                counts = dict(moved=outcomes.count("moved"), skipped=outcomes.count("skipped"),
                              failed=outcomes.count("failed"),
                              not_attempted=len(self.plan.get("operations", [])) - len(outcomes))
            detailed = str(self.status_label["text"])
            if detailed == f"状态：{STATE_EXECUTED}" or "已执行（无可撤销项）" in detailed:
                self.daily_status_label.configure(text=(f"已归档 {counts.get('moved', 0)} 个文件、"
                    f"{sum((v.get('result') or {}).get('outcome') == 'archived' for v in history['execution'].values())} 个空目录"
                    if self.plan.get('plan_type') == 'remaining_archive' else f"已整理 {counts.get('moved', 0)} 个文件"))
            problems = []
            undone_now = bool(self._chain_result and self._chain_result.reached)
            if not undone_now and any(counts.get(k, 0) for k in ("skipped", "failed", "not_attempted")):
                skipped = counts.get('skipped', 0)
                if not any(counts.get(k, 0) for k in ("failed", "not_attempted")):
                    reasons = [(slot.get("result") or {}).get("reason_code")
                               for slot in history["execution"].values()
                               if (slot.get("result") or {}).get("outcome") == "skipped"]
                    problems.append(f"{skipped} 个同名文件已存在，没有覆盖，原文件留在原处。"
                                    if reasons and all(r == "DEST_EXISTS" for r in reasons)
                                    else f"跳过 {skipped} 个文件，原文件留在原处；原因见左下「看原因」。")
                else:
                    problems.append(f"上次整理：跳过 {skipped}、失败 {counts.get('failed', 0)}、未处理 {counts.get('not_attempted', 0)} 个；请查看详情。")
            if self.plan.get('plan_type') == 'remaining_archive':
                if any(counts.get(k, 0) for k in ('dirs_skipped', 'dirs_failed', 'dirs_not_attempted')):
                    problems.append(f"剩余目录归档未全部完成：未满足 {counts.get('dirs_skipped', 0)}、"
                        f"失败/待检查 {counts.get('dirs_failed', 0)}、未处理 {counts.get('dirs_not_attempted', 0)} 个；请查看详情／恢复检查。")
            snapshot = history["snapshot"] or {}
            states = [e.get("undo_state") for e in snapshot.get("entries", [])]
            blocked = sum(str(s).startswith("blocked_") for s in states)
            if blocked:
                problems.append(f"有 {blocked} 个文件还原受阻；请查看详情。")
            if problems:
                self._state_problem = "\n".join(dict.fromkeys(self._state_problem.splitlines() + problems))
        elif has_plan and not current_plan:
            self.daily_status_label.configure(text="当前方案已失效 · 请重新生成预览")
            self._state_problem = "范围、类别或文件情况已变化，旧方案不能整理；请重新生成预览。"
        self._show_daily_problems()
        if self._chain_result is not None:
            result = self._chain_result
            if result.reached:
                dirs = f'、{result.restored_dirs} 个原目录' if result.restored_dirs else ''
                text = f'已还原 {result.restored} 个文件{dirs}到原位置' + (f' · 已退回 V{result.target}' if result.target else '')
            else:
                prefix = '撤销失败／需检查 · 已还原' if result.exception else '部分撤销 · 已还原'
                text = f'{prefix} {result.restored} 个文件、{result.restored_dirs} 个原目录 · 请查看提示'
            self.daily_status_label.configure(text=text)
        if self.session.active:
            head = self.session.active[-1]
            phase = read_plan_state(head.settings, head.plan_id).get('phase')
            unsafe = (phase in {'corrupted', 'undoing'}
                      or (phase in {'needs_review', 'unknown_needs_review', 'aborted_global',
                                    'aborted_log_failure', 'executing', 'locked'} and not head.checked)
                      or head.uncertain)
            self.undo_button.configure(state='disabled' if unsafe else 'normal')
        self.preview_button.configure(state='normal' if self._source_selected else 'disabled')
        self.remaining_button.configure(state='normal' if self._source_selected else 'disabled')
        self.watch_now_button.configure(state='normal' if self._source_selected else 'disabled')
        self.watch_check.configure(state='normal' if self._source_selected else 'disabled')
        if not self._source_selected:
            self.daily_status_label.configure(text='请先选择整理文件夹，再生成预览。')
            self.pick_button.configure(state='disabled')
        if self._operation_busy:
            for button in (self.source_button, self.preview_button, self.organize_button, self.confirm_button,
                           self.execute_button, self.undo_button, self.pick_button, self.recover_button,
                           self.return_button, self.return_zero_button, self.step_recover_button,
                           self.watch_now_button, self.watch_check, self.remaining_button):
                button.configure(state='disabled')
        else:
            self.source_button.configure(state='normal')
            self.pick_button.configure(state='normal' if self._source_selected and self.include_subfolders else 'disabled')
            self.step_recover_button.configure(state='normal')
            self._render_operations()
        self._history_view = history
        self._sync_daily_view(history, current_plan)

    def on_remaining_archive(self) -> None:
        if self._closing or self._operation_busy or not self._source_selected:
            return
        from .gui_remaining import RemainingArchiveDialog
        dialog = self._remaining_dialog
        if dialog is not None and dialog.window.winfo_exists():
            dialog.window.lift()
            return
        self._remaining_dialog = RemainingArchiveDialog(self)

    def _show_daily_problems(self) -> None:
        self.problem_label.configure(text="\n".join(filter(None, (self._preview_problem, self._state_problem,
            self._discovery_problem, getattr(self, '_chain_problem', '')))))
        self._sync_banner()

    # --- 扫描范围 -----------------------------------------------------------
    def scope_summary(self) -> str:
        if not self.include_subfolders or not self.selected_scope:
            return "扫描范围：仅顶层"
        picked = "，".join(self.selected_scope[:3])
        more = "" if len(self.selected_scope) <= 3 else f" 等 {len(self.selected_scope)} 个"
        return f"扫描范围：顶层 + {picked}{more}（递归）"

    def on_toggle_subfolders(self) -> None:
        if self._operation_busy or not self._source_selected:
            self.subfolder_var.set(self.include_subfolders)
            return
        self.include_subfolders = bool(self.subfolder_var.get())
        self.pick_button.configure(state="normal" if self.include_subfolders else "disabled")
        if not self.include_subfolders:
            self.selected_scope = []
        self._on_scope_changed()

    def on_pick_folders(self) -> None:
        if self._operation_busy or not self._source_selected:
            return
        try:
            folders = list_selectable_folders(self.settings.downloads_dir,
                target_parent_name=self.settings.target_parent_name)
        except Exception as exc:
            messagebox.showerror(WINDOW_TITLE, self._public(f'无法列出可选子文件夹：{exc}'))
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('选择要递归整理的子文件夹')
        dialog.geometry('560x420')
        dialog.minsize(500, 360)
        dialog.transient(self.root)
        ttk.Label(dialog, padding=8, wraplength=520,
            text='勾选后这些子文件夹内部的文件也会被整理；未选目录保持原样。\n'
                 '包目录、代码项目、空目录、隐藏与疑似敏感目录不会列出。').pack(fill='x')
        vars_by_path = {}
        self.folder_dialog = dialog
        self.folder_vars = vars_by_path
        owner_settings = self.settings
        actions = ttk.Frame(dialog, padding=8)
        actions.pack(side='bottom', fill='x')
        self.folder_all_button = ttk.Button(actions, text='全选',
            command=lambda: [v.set(True) for v in vars_by_path.values()])
        self.folder_all_button.pack(side='left')
        self.folder_none_button = ttk.Button(actions, text='取消全选',
            command=lambda: [v.set(False) for v in vars_by_path.values()])
        self.folder_none_button.pack(side='left', padx=8)
        body = ttk.Frame(dialog, padding=8)
        body.pack(fill='both', expand=True)
        body.rowconfigure(0, weight=1); body.columnconfigure(0, weight=1)
        canvas = tk.Canvas(body, highlightthickness=0)
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        vertical = ttk.Scrollbar(body, orient='vertical', command=canvas.yview)
        horizontal = ttk.Scrollbar(body, orient='horizontal', command=canvas.xview)
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        canvas.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        self.folder_canvas = canvas
        for folder in folders:
            var = tk.BooleanVar(value=folder.rel_path in self.selected_scope)
            vars_by_path[folder.rel_path] = var
            ttk.Checkbutton(inner, text=self._public(f'{folder.rel_path}（{folder.file_count} 个文件）'),
                            variable=var).pack(anchor='w')
        def confirm():
            if self._operation_busy or self.settings is not owner_settings:
                dialog.destroy()
                return
            self.selected_scope = [p for p in normalize_scope([p for p,v in vars_by_path.items() if v.get()]) if p]
            dialog.destroy()
            self._on_scope_changed()
        self.folder_confirm_button = ttk.Button(actions, text='确定', command=confirm)
        self.folder_confirm_button.pack(side='right')
        self.folder_cancel_button = ttk.Button(actions, text='取消', command=dialog.destroy)
        self.folder_cancel_button.pack(side='right', padx=8)

    def _on_scope_changed(self) -> None:
        """范围变化 → 旧确认失效；若已有计划则按新范围重新生成计划。"""
        self.scope_label.configure(text=self.scope_summary())
        self._invalidate_preview()
        if self.plan is not None:
            self.hash_label.configure(text="计划指纹：—（扫描范围已变化，需重新生成预览）")
        self._invalidate_discovery()
        self.watcher.reset(keep_reported=True)
        self._refresh_plan_buttons()
        if self.plan is not None:
            # 生成符合新范围的新计划，而不是只改界面标记
            self.on_preview()

    # --- 自动检查新文件 ------------------------------------------------------
    def on_toggle_watch(self) -> None:
        if self._operation_busy or not self._source_selected:
            self._watch_enabled_var.set(False)
            return
        self._invalidate_discovery()
        if self._watch_enabled_var is not None and self._watch_enabled_var.get():
            self.watcher.enable()
            self.watch_label.configure(text="自动检查：已开启（仅提示，不会自动移动或确认）")
            self._schedule_watch()
        else:
            self.watcher.disable()
            self._cancel_watch_job()
            self.watch_label.configure(text="自动检查：已关闭（不会自动移动或确认）")
            self._set_watch_text("自动检查已关闭；未做任何移动。")

    def _cancel_watch_job(self) -> None:
        if self.watch_job is not None:
            try:
                self.root.after_cancel(self.watch_job)
            except Exception:  # noqa: BLE001
                pass
            self.watch_job = None

    def _schedule_watch(self) -> None:
        self._cancel_watch_job()
        if not self.watcher.enabled or self._closing:
            return
        self.watch_job = self.root.after(
            int(self.watcher.interval_seconds * 1000), self._on_watch_tick
        )

    def _on_watch_tick(self) -> None:
        self.watch_job = None
        if not self.watcher.enabled or self._closing:
            return
        self.on_check_now(schedule_next=True)

    def _known_paths(self) -> set[str]:
        if self.plan is None:
            return set()
        return {str(op.get("rel_path") or "") for op in self.plan.get("operations", [])}

    def _init_discovery_worker(self) -> None:
        self._discovery_thread = None
        self._discovery_cancel = None
        self._discovery_queue = queue.SimpleQueue()
        self._discovery_generation = 0
        self._discovery_poll_job = None

    def _invalidate_discovery(self) -> None:
        if not hasattr(self, "_discovery_generation"):
            self._init_discovery_worker()
        self._discovery_generation += 1
        if self._discovery_cancel is not None:
            self._discovery_cancel.set()

    def on_check_now(self, *, schedule_next: bool = False) -> None:
        if self._operation_busy or not self._source_selected:
            return
        """单个只读工作线程；Tk 与观察状态发布只在主线程完成。"""
        if not hasattr(self, "_discovery_generation"):
            self._init_discovery_worker()
        if self._closing:
            return
        if self._discovery_thread is not None:
            return  # 前一任务尚未回收，不能堆积新的扫描
        self._cancel_watch_job()
        token = self._discovery_generation
        cancel = threading.Event()
        self._discovery_cancel = cancel
        watcher = copy.copy(self.watcher)
        watcher._tracked = copy.deepcopy(self.watcher._tracked)
        watcher._reported = set(self.watcher._reported)
        # bound method 引用 Tk App，避免 deepcopy 整个 UI。
        watcher._clock = self.watcher._clock
        opts = dict(downloads_dir=self.settings.downloads_dir,
                    target_parent_name=self.settings.target_parent_name,
                    scope_dirs=list(self.selected_scope),
                    recursive=self.include_subfolders and bool(self.selected_scope),
                    extra_ignored=self._known_paths(), cancel_event=cancel)
        output = self._discovery_queue

        def run():
            try:
                report = watcher.check_once(**opts)
                value = (token, watcher, report, None)
            except Exception as exc:
                value = (token, watcher, None, type(exc).__name__)
            output.put(value)  # 无 Tk 调用；关闭后返回的结果不会发布

        self._set_watch_text("自动检查中（仅提示，不会自动移动或确认）…")
        self._discovery_thread = threading.Thread(target=run, name="doc-selecter-discovery", daemon=True)
        self._discovery_thread.start()
        self._discovery_poll_job = self.root.after(10, self._poll_discovery)

    def _poll_discovery(self) -> None:
        self._discovery_poll_job = None
        if self._closing:
            return
        try:
            token, watcher, report, error = self._discovery_queue.get_nowait()
        except queue.Empty:
            self._discovery_poll_job = self.root.after(10, self._poll_discovery)
            return
        # 线程可能刚发出结果尚未退出；退出后才允许下一任务。
        if self._discovery_thread.is_alive():
            self._discovery_queue.put((token, watcher, report, error))
            self._discovery_poll_job = self.root.after(10, self._poll_discovery)
            return
        self._discovery_thread = None
        self._discovery_cancel = None
        if token == self._discovery_generation:
            if error:
                self._set_watch_text(f"自动检查出错（未做任何移动）：{error}")
            else:
                self.watcher = watcher
                text = NewFileWatcher.format_report(report)
                if report.new_files:
                    text += "\n稳定只是启发式，不保证下载完成。已有计划不会被改动；请生成预览再确认。"
                self._set_watch_text(text)
        if self.watcher.enabled:
            self._schedule_watch()

    def _set_watch_text(self, text: str) -> None:
        text = self._public(text)
        self.discovery_detail.configure(state="normal")
        self.discovery_detail.delete("1.0", "end")
        self.discovery_detail.insert("1.0", text)
        self.discovery_detail.configure(state="disabled")
        names = [line.strip()[2:] for line in text.splitlines() if line.strip().startswith("+ ")]
        errors = [line.strip() for line in text.splitlines() if any(word in line for word in ("出错", "覆盖不完整", "读取失败", "扫描变化"))]
        self._discovery_problem = "新文件检查覆盖不完整或出错，请查看详情并重新生成预览。" if errors else ""
        self._show_daily_problems()
        if names or errors or "检查中" in text:
            self.watch_results.grid(row=2, column=0, sticky="ew", pady=(6, 0))
            if names:
                short = "发现新文件 · 请重新生成预览\n" + "\n".join(names[:6])
                self.watch_more_button.grid(row=3, column=0, sticky="w") if len(names) > 6 else self.watch_more_button.grid_remove()
                text = short
            elif not errors:
                text = "正在检查新文件（仅提示，不会移动）…"
        else:
            self.watch_results.grid_remove()
            self.watch_more_button.grid_remove()
        self.watch_text.configure(state="normal")
        self.watch_text.delete("1.0", "end")
        self.watch_text.insert("1.0", text)
        self.watch_text.yview_moveto(0)
        self.watch_text.configure(state="disabled")
        self._sync_banner()

    def on_close(self) -> None:
        """关闭窗口：先清理定时任务与观察状态，再销毁窗口。"""
        if self._operation_busy:
            return
        self._hide_tooltip()
        self._closing = True
        preparation = getattr(self, '_remaining_preparation', None)
        if preparation is not None:
            preparation.close()
        remaining = self._remaining_dialog
        if remaining is not None:
            remaining.close()
        self._invalidate_discovery()
        if self._discovery_poll_job is not None:
            self.root.after_cancel(self._discovery_poll_job)
            self._discovery_poll_job = None
        self._cancel_watch_job()
        self.watcher.disable()
        try:
            self.details_window.destroy()
            self.settings_window.destroy()
            self.operations_window.destroy()
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    # --- 操作 ---------------------------------------------------------------
    def on_preview(self) -> None:
        if self._operation_busy or not self._source_selected:
            return
        self._chain_result = None
        self._invalidate_preview()
        self._invalidate_discovery()
        self.watcher.reset(keep_reported=True)
        try:
            assert_downloads_exists(self.settings)
        except ConfigError as exc:
            messagebox.showerror(WINDOW_TITLE, self._public(exc))
            self._set_status(STATE_ERROR)
            return
        try:
            build = build_plan(
                self.settings,
                category_selection=self._selected_categories() if self._selection_vars else None,
                scope_dirs=self.selected_scope,
                recursive=self.include_subfolders and bool(self.selected_scope),
                archive_emptied_dirs=self.archive_var.get(),
            )
            write_plan(self.settings, build.plan)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(WINDOW_TITLE, f"生成预览失败：{self._public(exc)}")
            self._set_status(STATE_ERROR)
            return
        self.plan = build.plan
        self.confirmed_hash = None
        self._render_plan(build.plan)
        self._preview_ready = True
        self._refresh_plan_buttons()
        self._set_status(STATE_PREVIEWED)

    def _render_plan(self, plan: dict[str, Any]) -> None:
        plan = public_plan_view(plan, self.settings)
        self._render_categories(plan["category_selection"],
            plan.get("category_totals", {}).get("counts", {}),
            plan.get("category_totals", {}).get("sizes", {}))

        # 明细
        for item in self.tree.get_children():
            self.tree.delete(item)
        for item in self.detail_tree.get_children():
            self.detail_tree.delete(item)
        for op in plan.get("operations", []):
            relative_dest = os.path.relpath(op["dest"], self.settings.downloads_dir)
            # 显示目标父链，原文件名不改；同名源由相对路径区分。
            folder = os.path.dirname(relative_dest)
            self.detail_tree.insert("", "end", values=(
                op.get("rel_path") or os.path.basename(op["source"]), op["source"], op["dest"],
                op["classification_basis"], human_size(int(op["identity"]["size_bytes"]))))
            self.tree.insert(
                "", "end",
                values=(
                    op.get("rel_path") or os.path.basename(op["source"]),
                    op["source"], op["dest"], op["classification_basis"],
                    human_size(int(op["identity"]["size_bytes"])), folder,
                ),
            )

        directory_lines = []
        for op in plan.get("directory_operations", []):
            directory_lines.append(f"{op['rel_path']} → {op['dest']}\n  "
                + ('原本空目录 · 独立选择' if plan.get('plan_type') == 'remaining_archive'
                   else f"依赖：{', '.join(op['depends_on'])}"))
            if plan.get('plan_type') == 'remaining_archive':
                self.detail_tree.insert('', 'end', values=(op['rel_path'], op['source'], op['dest'], '原本空目录', '—'))
                self.tree.insert('', 'end', values=(op['rel_path'], op['source'], op['dest'], '原本空目录', '—',
                    os.path.dirname(os.path.relpath(op['dest'], self.settings.downloads_dir))))
        self.directory_text.configure(state="normal")
        self.directory_text.delete("1.0", "end")
        self.directory_text.insert("1.0", "\n".join(directory_lines) or "（无条件候选）")
        self.directory_text.configure(state="disabled")
        self.archive_label.configure(text=(f"可能归档目录 {len(plan.get('directory_operations', []))} 个，执行后以实际空目录为准"
            if plan.get("archive", {}).get("enabled") else ""))
        if plan.get('plan_type') == 'remaining_archive':
            self.archive_label.configure(text=f"剩余归档 · 原本空目录 {len(plan.get('directory_operations', []))} 个")
        # 跳过清单
        lines: list[str] = []
        excluded = plan.get("excluded", {})
        self._render_remaining(plan)
        problems = []
        for label in ("元数据读取失败", "递归读取失败/扫描变化"):
            value = excluded.get(label, {})
            if value.get("count", 0):
                problems.append(f"有 {value['count']} 处读取失败或扫描变化，预览可能不完整；请查看详情并重新生成预览。")
        self._preview_problem = "\n".join(problems)
        for label, value in excluded.items():
            if label == "跳过原因":
                for kind, reasons in value.items():
                    lines.append(f"{kind}：{'；'.join(map(str, reasons))}")
            elif label == '未归档原因数量':
                for reason, count in value.items():
                    lines.append(f'{reason}：{count} 个')
            elif isinstance(value, dict):
                if value.get("names", "missing") is None:
                    lines.append(f"{label}：{value.get('count', 0)} 个（按隐私规则不展示名称）")
                else:
                    lines.append(f"{label}：{value.get('count', 0)} 个")
                    for issue in value.get("items", []):
                        lines.append(f"  {issue.get('path', '')}：{issue.get('detail', issue.get('code', ''))}")
                    for kind, count in value.get("types", {}).items():
                        lines.append(f"  {kind}：{count} 个")
            else:
                lines.append(f"{label}：{len(value)} 个")
                lines.extend(f"  {name}" for name in value)
        self.skip_text.configure(state="normal")
        self.skip_text.delete("1.0", "end")
        self.skip_text.insert("1.0", "\n".join(lines) or "（无跳过项）")
        self.skip_text.configure(state="disabled")

        counts_all = plan.get("counts", {})
        total_size = sum(
            int(op["identity"]["size_bytes"]) for op in plan.get("operations", [])
        )
        plan_id = str(plan["plan_id"])
        scope_desc = "顶层" if not plan.get("recursive") else (
            "顶层 + " + "，".join([x for x in plan.get("scan_scope", []) if x][:3])
        )
        self.plan_label.configure(
            text=f"计划编号：{plan_id}    扫描范围：{scope_desc}"
            f"（{len(plan.get('scan_scope', []))} 个目录）"
        )
        self.hash_label.configure(text=f"计划指纹：{plan['plan_hash']}（指纹变化 = 需重新确认）")
        self.summary_label.configure(
            text=(
                f"方案：移动 {counts_all.get('selected', 0)} 个文件 / 合计 "
                f"{human_size(total_size)}（逻辑大小，不等于可释放磁盘空间）"
            )
        )
        self._refresh_plan_buttons()

    def _ask_move(self, short: bool = False) -> bool:
        from .directory_archive import RISK_NOTICE
        archive_notice = (f"条件目录归档：{len(self.plan.get('directory_operations', []))} 个\n"
            f"归档根：{self._public(self.settings.downloads_dir + '/' + self.plan['archive']['relative_root'])}\n"
            f"{RISK_NOTICE}\n\n" if self.plan.get('archive', {}).get('enabled') else "")
        if short:
            scope = ("" if not self.include_subfolders or not self.selected_scope else
                     f"（顶层 + {self._public('、'.join(self.selected_scope))}，含其子文件夹）")
            return messagebox.askokcancel(
                "确认整理", f"整理 {len(self.plan.get('operations', []))} 个文件？",
                detail=(archive_notice + f"文件会移到 {self._public(os.path.basename(self.settings.downloads_dir))}{scope}"
                        f" 里的「{self._public(self.settings.target_parent_name)}」对应文件夹。\n"
                        "同名文件不会被覆盖，整理后可以撤销。"))
        return messagebox.askokcancel(
            "确认整理",
            "即将移动以下文件：\n\n"
            f"  数量：{len(self.plan.get('operations', []))} 个\n"
            f"  范围：{self._public('仅顶层' if not self.include_subfolders or not self.selected_scope else '顶层 + ' + '、'.join(self.selected_scope) + '（递归）')}\n"
            f"  目标根目录：{self._public(self.settings.target_root)}\n\n"
            + archive_notice + "目标重名一律跳过，绝不覆盖。\n继续整理？")

    def on_organize(self) -> None:
        if self._operation_busy:
            return
        """日常入口：用户最终同意后才保存确认，仍经原执行入口的完整绑定核对。"""
        if not self._plan_matches_current():
            self._invalidate_preview()
            messagebox.showwarning(WINDOW_TITLE, "请先重新生成有效预览，再确认整理。")
            return
        if not self.plan.get("operations"):
            self._refresh_plan_buttons()
            messagebox.showwarning(WINDOW_TITLE, "请先重新生成有效预览，再确认整理。")
            return
        approved_plan = self.plan
        approved_hash = approved_plan["plan_hash"]
        if not self._ask_move(short=True):
            return
        # 模态期间控件变化、重新预览或旧计划被消耗都不能扩大这次授权。
        if self.plan is not approved_plan or not self._plan_matches_current() or self.plan["plan_hash"] != approved_hash:
            self._invalidate_preview()
            self._set_status("方案已失效，请重新生成预览")
            return
        if not self._confirm_saved_plan(notify=False):
            return
        result = self.perform_execution()
        if result is not None:
            self._report_execution(result, notify=False)

    def on_execute(self) -> None:
        if self._operation_busy:
            return
        if self.plan is None:
            return
        if self._read_plan_history()["consumed"]:
            self._refresh_plan_buttons()
            messagebox.showwarning(WINDOW_TITLE, "历史计划已执行或需要检查，请重新生成预览并明确确认。")
            return
        if not self._plan_matches_current():
            self._invalidate_preview()
            messagebox.showwarning(WINDOW_TITLE, "当前范围或类别没有对应的已保存预览，请重新生成预览并确认。")
            return
        plan_hash = str(self.plan["plan_hash"])
        if self.confirmed_hash != plan_hash:
            messagebox.showwarning(WINDOW_TITLE, "方案尚未确认或已变化，请先点击「确认方案」。")
            return
        approved_plan = self.plan
        approved_hash = self.plan["plan_hash"]
        ok = self._ask_move()
        if not ok:
            return
        if self.plan is not approved_plan or self.plan["plan_hash"] != approved_hash:
            self._invalidate_preview()
            self._set_status("方案已失效，请重新生成预览")
            return
        result = self.perform_execution()
        if result is None:
            return
        self._report_execution(result)

    def _report_execution(self, result, notify: bool = True) -> None:
        """notify=False（日常主按钮）：全部成功时不再弹完成提示，结果只写在顶部结果条。"""
        quiet = not notify and not (result.failed or result.not_attempted
                                    or result.dirs_failed or result.dirs_not_attempted)
        if result.stopped:
            messagebox.showwarning(
                WINDOW_TITLE,
                f"执行已停止：{result.stop_reason}\n"
                f"其余 {result.not_attempted} 个文件未处理；目录成功 {result.archived_dirs}、失败/待检查 {result.dirs_failed}、未处理 {result.dirs_not_attempted}。请查看详情。",
            )
        elif not quiet:
            messagebox.showinfo(
                WINDOW_TITLE,
                f"本次共移动 {result.moved} 个文件。\n"
                + (f"目录归档 {result.archived_dirs}、条件未满足 {result.dirs_skipped}、失败/待检查 {result.dirs_failed}、未处理 {result.dirs_not_attempted}。\n" if self.plan.get("archive",{}).get("enabled") else "")
                + f"跳过 {result.skipped}、失败 {result.failed}、未处理 {result.not_attempted} 个。\n{FOOTER_WARNING}",
            )
        if (result.archived_dirs or result.dirs_skipped or result.dirs_failed or result.dirs_not_attempted):
            self.archive_label.configure(text=f"目录归档：成功 {result.archived_dirs}、条件未满足 {result.dirs_skipped}、失败 {result.dirs_failed}、未处理 {result.dirs_not_attempted}")
        self._set_status(STATE_EXECUTED)
        self._refresh_plan_buttons()

    def perform_execution(self):
        """真正执行移动并更新界面（不含模态弹窗）。

        与「确认对话框」解耦：正常使用时由 :meth:`on_execute` 在用户确认后调用；
        自动化验证可在独立子进程里直接调用，从而走**同一条真实执行路径**
        （macOS 的原生 ``tk_messageBox`` 不返回 Tcl 事件循环，无法被脚本关闭）。
        """
        if self._operation_busy or self.plan is None:
            return None
        # 二次弹窗返回后也核对，不能绕过当前控件与已保存计划的绑定。
        if not self._plan_matches_current():
            self._invalidate_preview()
            return None
        plan_hash = str(self.plan["plan_hash"])
        if self.confirmed_hash != plan_hash:
            return None
        settings, plan_id = self.settings, str(self.plan["plan_id"])
        self._operation_busy = True
        self._refresh_plan_buttons()
        try:
            result = execute_plan(
                self.settings, str(self.plan["plan_id"]), plan_hash, kind="gui"
            )
        except (PlanError, ExecutionRefused, LockBusy, OSError) as exc:
            messagebox.showerror(WINDOW_TITLE, self._public(exc))
            self._set_status(STATE_ERROR)
            return None
        finally:
            try:
                self.session.track(settings, plan_id)
            finally:
                self._operation_busy = False
                self._refresh_plan_buttons()
        self.result_label.configure(
            text=(
                f"上次执行：成功 {result.moved} · 跳过 {result.skipped} · "
                f"失败 {result.failed} · 未处理 {result.not_attempted}"
                + (f"\n目录归档：成功 {result.archived_dirs} · 未满足 {result.dirs_skipped} · 失败/待检查 {result.dirs_failed} · 未处理 {result.dirs_not_attempted}" if self.plan.get("archive",{}).get("enabled") else "")
            )
        )
        self._refresh_plan_buttons()
        return result

    def on_confirm(self) -> None:
        """保留详情中的原分步确认入口。"""
        self._confirm_saved_plan(notify=True)

    def _confirm_saved_plan(self, *, notify: bool) -> bool:
        if self._operation_busy or not self._source_selected:
            return False
        """确认方案（锁定计划指纹），不改变原确认记录契约。"""
        if self.plan is None:
            messagebox.showinfo(WINDOW_TITLE, "请先生成预览。")
            return False
        if self._read_plan_history()["consumed"]:
            self._refresh_plan_buttons()
            messagebox.showwarning(WINDOW_TITLE, "历史计划已执行或需要检查，请重新生成预览并明确确认。")
            return False
        if (self._preview_ready and self._plan_scope_matches_current()
                and self._selected_categories() != self.plan["category_selection"]):
            # 保留类别变化的既有产品路径：先成功保存新计划，再确认。
            self.on_preview()
        if not self._plan_matches_current():
            self._invalidate_preview()
            messagebox.showwarning(WINDOW_TITLE, "当前范围或类别没有对应的已保存预览，请重新生成预览后确认。")
            return False
        try:
            write_confirmation(self.settings, str(self.plan["plan_id"]), str(self.plan["plan_hash"]))
        except (PlanError, OSError) as exc:
            self._invalidate_preview()
            messagebox.showerror(WINDOW_TITLE, self._public(exc))
            return False
        self.confirmed_hash = str(self.plan["plan_hash"])
        self._set_status(STATE_CONFIRMED)
        self._refresh_plan_buttons()
        if notify:
            messagebox.showinfo(WINDOW_TITLE,
                "已确认该方案。任何改动都会使确认失效，需重新确认。\n现在可以点击「执行移动」。")
        return True

    def on_undo(self) -> None:
        if self._operation_busy:
            return
        if self.session.active:
            self.on_return_to(self.session.active[-1].parent, simple=True)
            return
        if self.plan is None:
            return
        self._operation_busy = True
        self._invalidate_discovery()
        self._refresh_plan_buttons()
        ok = None
        try:
            ok = self._undo_history()
        finally:
            self._operation_busy = False
            self._refresh_plan_buttons()
        if ok is False:
            self._set_status('本次撤销失败（未确认全部还原）')
            self.result_label.configure(text=str(self.result_label['text']) + '（本次撤销失败）')

    def _undo_history(self):
        if not messagebox.askokcancel(
            "确认撤销",
            f"先还原已归档的原目录及父链，再把已移动文件移回原位置；一批未完整还原即停止。\n\n{FOOTER_WARNING}\n\n继续？",
        ):
            return
        try:
            result = undo_plan(self.settings, str(self.plan["plan_id"]), confirm=True, kind="gui")
        except (PlanError, UndoRefused, ExecutionRefused, LockBusy, OSError) as exc:
            messagebox.showerror(WINDOW_TITLE, self._public(exc))
            return False
        messagebox.showinfo(
            WINDOW_TITLE,
            f"原目录还原 {result.restored_dirs}；文件已还原 {result.restored} · 被阻止 {result.blocked} · "
            f"无需还原 {result.skipped_not_moved} · 不可判 {result.unknown}\n"
            f"{'本次撤销完成。' if result.undo_status == 'completed' else '本次未能全部还原，请查看详情，必要时使用恢复/检查。'}\n\n{FOOTER_WARNING}",
        )
        self._refresh_plan_buttons()
        return True

    def on_recover(self) -> None:
        if self._operation_busy:
            return
        if self.plan is None:
            return
        self._operation_busy = True
        self._invalidate_discovery()
        self._refresh_plan_buttons()
        ok = None
        try:
            ok = self._recover_current()
        finally:
            self._operation_busy = False
            self._refresh_plan_buttons()
        if ok is False:
            self._set_status('恢复/检查失败（请检查记录）')

    def _recover_current(self):
        try:
            report = recover_execution(self.settings, str(self.plan["plan_id"]), kind="gui")
            self.session.checked_batch(self.settings, str(self.plan['plan_id']))
        except (PlanError, ExecutionRefused, LockBusy, OSError) as exc:
            messagebox.showerror(WINDOW_TITLE, self._public(exc))
            return False
        messagebox.showinfo(
            WINDOW_TITLE,
            f"阶段：{report['phase']}\n"
            f"补记已完成：{report.get('recovered') or '无'}\n"
            f"不可判：{report.get('unknown') or '无'}\n"
            + self._public("\n".join(report.get("notes", []))),
        )
        self._refresh_plan_buttons()
        return True

    def _load_latest_plan_if_any(self) -> None:
        if not self._source_selected:
            return
        plans = list_plans(self.settings)
        if not plans:
            return
        try:
            def candidates():
                for plan_id in reversed(plans):
                    try:
                        candidate = load_verified_plan(self.settings, plan_id, display_only=True)
                    except (PlanError, KeyError, TypeError, ValueError, OSError):
                        continue
                    if candidate.get('downloads_dir') == self.settings.canonical_downloads_dir:
                        yield candidate, self._read_plan_history(candidate)
            plan = latest_restorable_plan(candidates())
            if plan is None:
                return
        except Exception:  # noqa: BLE001
            return
        self.plan = plan
        # 旧显式范围仍保留在不可变计划及详情中；选择来源后当前范围保持顶层。
        # 不用历史 selected_scope 或展开 scan_scope 自动勾选新操作的授权。
        self.scope_label.configure(text=self.scope_summary())
        self._render_plan(plan)
        self.remaining_label.configure(text='')
        self._preview_ready = False
        self.confirmed_hash = None  # 恢复展示从不等同于自动确认
        self._set_status("历史方案（请重新生成预览；仍可撤销）")
        self._refresh_plan_buttons()


def _build_root() -> tk.Tk:
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # 无显示环境
        raise RuntimeError(f"无法启动图形界面（没有可用显示？）：{exc}") from exc
    return root


def build_app(settings: Settings | None = None) -> tuple[tk.Tk, App]:
    """构造窗口（供测试与入口共用）。"""
    settings = settings or load_settings()
    ensure_state_dirs(settings)
    root = _build_root()
    root.title(WINDOW_TITLE)
    app = App(root, settings)
    return root, app


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    try:
        root, _app = build_app(settings)
    except RuntimeError as exc:
        print(str(exc))
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
