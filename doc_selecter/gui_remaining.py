"""Separate, explicit remaining archive UI; all movement goes through core APIs."""
import os
import copy
import queue
import threading
from dataclasses import dataclass, field
import tkinter as tk
from tkinter import ttk, messagebox

from .directory_archive import RISK_NOTICE
from .remaining_archive import discover_remaining, build_remaining_plan
from .planner import write_plan, write_confirmation
from .executor import execute_plan


@dataclass(eq=False)
class PreparationTask:
    """Plain worker input: no widget, Tcl variable or Tk callback."""
    kind: str
    settings: object
    context: tuple
    revision: int
    arguments: dict
    cancelled: threading.Event = field(default_factory=threading.Event)
    save_gate: threading.Lock = field(default_factory=threading.Lock)
    saving: bool = False

    def cancel(self):
        # The lock only protects the start decision, NEVER blocking filesystem I/O.
        with self.save_gate:
            self.cancelled.set()


def _prepare(task, output):
    """Worker never calls Tk, including after()/winfo_exists()."""
    value, error = None, None
    try:
        if not task.cancelled.is_set():
            if task.kind == 'discovery':
                value = discover_remaining(task.settings, **task.arguments)
            else:
                value = build_remaining_plan(task.settings, **task.arguments).plan
                with task.save_gate:
                    if task.cancelled.is_set():
                        return
                    task.saving = True
                # Once started, retain immutable evidence even if the UI cancels.
                write_plan(task.settings, value)
    except Exception as exc:
        error = str(exc)
    finally:
        output.put((task, value, error))


class RemainingPreparation:
    """One running read/write preparation and one replaceable latest request per App.

    All scheduler state and publishing run on Tk's thread. Closing only cancels
    tokens/polling; it never joins or interrupts a filesystem call.
    """
    def __init__(self, owner):
        self.owner = owner
        self.output = queue.SimpleQueue()
        self.active = None
        self.pending = None
        self.poll_job = None
        self.closed = False

    def submit(self, receiver, task):
        self.cancel(receiver)
        if self.closed:
            task.cancel()
            return
        if self.active is None:
            self._start(receiver, task)
        else:
            if self.pending:
                self.pending[1].cancel()
            self.pending = (receiver, task)

    def _start(self, receiver, task):
        self.active = (receiver, task)
        try:
            threading.Thread(target=_prepare, args=(task, self.output),
                             name='doc-selecter-remaining-preparation', daemon=False).start()
        except Exception as exc:
            self.output.put((task, None, str(exc)))
        if self.poll_job is None:
            self.poll_job = self.owner.root.after(10, self._poll)

    def cancel(self, receiver):
        for pair in (self.active, self.pending):
            if pair and pair[0] is receiver:
                pair[1].cancel()
        if self.pending and self.pending[0] is receiver:
            self.pending = None

    def _poll(self):
        self.poll_job = None
        if self.closed or self.owner._closing:
            return
        for pair in (self.active, self.pending):
            if pair and not pair[1].cancelled.is_set() and not pair[0].task_matches(pair[1]):
                pair[1].cancel()
                if pair[0]._task is pair[1] and not pair[0]._closed:
                    pair[0].invalidate_context()
        if self.owner._operation_busy:
            self.poll_job = self.owner.root.after(10, self._poll)
            return  # Keep movement/undo mutual exclusion, including modal loops.
        while True:
            try:
                task, value, error = self.output.get_nowait()
            except queue.Empty:
                break
            if not self.active or self.active[1] is not task:
                continue  # Duplicate/late completions cannot displace current work.
            receiver, _ = self.active
            self.active = None
            if not task.cancelled.is_set() and receiver.task_matches(task):
                receiver.prepared(task, value, error)
        if self.active is None and self.pending:
            receiver, task = self.pending
            self.pending = None
            if not task.cancelled.is_set() and receiver.task_matches(task):
                self._start(receiver, task)
        if self.active is not None and self.poll_job is None:
            self.poll_job = self.owner.root.after(10, self._poll)

    def close(self):
        self.closed = True
        for pair in (self.active, self.pending):
            if pair:
                pair[1].cancel()
        self.pending = None
        if self.poll_job is not None:
            self.owner.root.after_cancel(self.poll_job)
            self.poll_job = None
        # No worker callback uses owner/receiver; late results stay in the queue.
        self.active = None
        self.owner = None


class RemainingArchiveDialog:
    def __init__(self, owner):
        self.owner = owner
        self.window = tk.Toplevel(owner.root)
        self.window.title('归档剩余项目')
        self.window.geometry('960x720')
        self.window.minsize(760, 620)
        self.window.transient(owner.root)
        self.plan = None
        self.variables = {}
        self._candidate_traces = []
        self.discovery = None
        self.context = None
        self.settings = owner.settings
        self._revision = 0
        self._task = None
        self._closed = False
        if getattr(owner, '_remaining_preparation', None) is None:
            owner._remaining_preparation = RemainingPreparation(owner)
        self.preparation = owner._remaining_preparation
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(2, weight=1)
        self.window.rowconfigure(4, weight=1)
        self.source_label = ttk.Label(self.window, wraplength=730, padding=8)
        self.source_label.grid(row=0, column=0, sticky='ew')
        actions = ttk.Frame(self.window, padding=8)
        actions.grid(row=1, column=0, sticky='ew')
        self.all_button = ttk.Button(actions, text='全选可归档项', command=lambda: self.select_all(True))
        self.all_button.pack(side='left')
        self.none_button = ttk.Button(actions, text='取消全选', command=lambda: self.select_all(False))
        self.none_button.pack(side='left', padx=8)
        self.refresh_button = ttk.Button(actions, text='重新发现', command=self.refresh_candidates)
        self.refresh_button.pack(side='right')
        body = ttk.LabelFrame(self.window, text='逐项勾选 · 默认不选择', padding=8)
        body.grid(row=2, column=0, sticky='nsew', padx=8)
        body.columnconfigure(0, weight=1); body.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(body, highlightthickness=0)
        self.inner = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.inner, anchor='nw')
        self.inner.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        vertical = ttk.Scrollbar(body, orient='vertical', command=self.canvas.yview)
        horizontal = ttk.Scrollbar(body, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.canvas.grid(row=0, column=0, sticky='nsew'); vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        ttk.Label(self.window, text='归档预览 · 保留原名及相对位置，不删除、不重新分类', padding=8).grid(row=3, column=0, sticky='w')
        preview = ttk.Frame(self.window, padding=8)
        preview.grid(row=4, column=0, sticky='nsew')
        preview.rowconfigure(0, weight=1); preview.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(preview, columns=('source', 'dest'), show='headings', height=7)
        self.tree.heading('source', text='原位置'); self.tree.heading('dest', text='归档去向')
        self.tree.column('source', width=430, minwidth=300); self.tree.column('dest', width=600, minwidth=400)
        vs = ttk.Scrollbar(preview, orient='vertical', command=self.tree.yview)
        hs = ttk.Scrollbar(preview, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky='nsew'); vs.grid(row=0, column=1, sticky='ns'); hs.grid(row=1, column=0, sticky='ew')
        self.status = ttk.Label(self.window, wraplength=730, padding=8)
        self.status.grid(row=5, column=0, sticky='ew')
        footer = ttk.Frame(self.window, padding=8)
        footer.grid(row=6, column=0, sticky='ew')
        self.preview_button = ttk.Button(footer, text='生成归档预览', command=self.preview, state='disabled')
        self.preview_button.pack(side='left')
        self.confirm_button = ttk.Button(footer, text='确认归档', command=self.confirm, state='disabled')
        self.confirm_button.pack(side='left', padx=8)
        self.close_button = ttk.Button(footer, text='关闭', command=self.close)
        self.close_button.pack(side='right')
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        # Capture even direct setting variable changes before a worker save gate.
        self._control_traces = []
        for var in (owner.subfolder_var, owner.archive_var, owner._watch_enabled_var,
                    *owner._selection_vars.values()):
            self._control_traces.append((var, var.trace_add('write', self._controls_changed)))
        self.refresh_candidates()

    def _controls_changed(self, *_):
        if not self._closed:
            self.invalidate_context()

    def close(self):
        if not self._closed and not self.owner._operation_busy:
            self._closed = True
            self.preparation.cancel(self)
            self._task = None
            self.plan = None
            for var, token in self._control_traces:
                var.trace_remove('write', token)
            self._control_traces.clear()
            self._clear_candidates()
            self.window.destroy()
            self.owner = None

    def _clear_candidates(self):
        # Release our Tcl variable callbacks on Tk's thread, including refresh.
        for var, token in self._candidate_traces:
            var.trace_remove('write', token)
        self._candidate_traces.clear()
        self.variables = {}

    def invalidate_context(self):
        if self._closed:
            return
        self._revision += 1
        self.preparation.cancel(self)
        self._task = None
        self.plan = None
        self.confirm_button.configure(state='disabled')
        self.preview_button.configure(state='disabled')
        self.status.configure(text='来源、范围或设置已变化 · 请重新发现并勾选')
        self.refresh_button.configure(state='normal')

    def fingerprint(self):
        o = self.owner
        return (id(o.settings), o._source_selected, o.include_subfolders,
                tuple(o.selected_scope), bool(o.subfolder_var.get()),
                tuple(o._selected_categories()), bool(o.archive_var.get()),
                bool(o._watch_enabled_var.get()), o._remaining_epoch)

    def selection(self):
        return sorted(p for p, var in self.variables.items() if var.get())

    def selection_changed(self, *_):
        if self._closed:
            return
        self._revision += 1
        self.preparation.cancel(self)
        self._task = None
        self.plan = None
        self.confirm_button.configure(state='disabled')
        self.preview_button.configure(state='normal' if self.valid_context() and self.selection() and self.discovery and not self.discovery.issues else 'disabled')
        self.status.configure(text='勾选已变化 · 请生成归档预览' if self.selection() else '尚未勾选 · 不会归档任何项目')
        for item in self.tree.get_children():
            self.tree.delete(item)

    def select_all(self, selected):
        if self._closed or self.owner._operation_busy:
            return
        for var in self.variables.values():
            var.set(selected)

    def refresh_candidates(self):
        o = self.owner
        if self._closed or o._closing or o._operation_busy or not o._source_selected:
            return
        if self._task and self._task.kind == 'discovery' and self.task_matches(self._task):
            return
        self._revision += 1
        self.plan = None
        self.discovery = None
        self._clear_candidates()
        self.context = None
        for child in self.inner.winfo_children():
            child.destroy()
        self.confirm_button.configure(state='disabled')
        self.preview_button.configure(state='disabled')
        self.settings = o.settings
        self.status.configure(text='正在发现可归档项目… 可关闭窗口')
        self.refresh_button.configure(state='disabled')
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.source_label.configure(text=o._public(f'来源：{self.settings.downloads_dir}\n{o.scope_summary()}；归档到该来源内的「{self.settings.target_parent_name}/已归档」'))
        task = PreparationTask('discovery', self.settings, self.fingerprint(), self._revision,
            {'scope_dirs': list(o.selected_scope), 'recursive': bool(o.include_subfolders and o.selected_scope)})
        self._task = task
        self.preparation.submit(self, task)

    def task_matches(self, task):
        return (not self._closed and not self.owner._closing and self._task is task
                and self._revision == task.revision and self.fingerprint() == task.context
                and self.owner.settings is task.settings
                and (task.kind != 'preview' or self.selection() == task.arguments['selection']))

    def prepared(self, task, value, error):
        self._task = None
        self.refresh_button.configure(state='normal')
        if error is not None:
            self.plan = None
            self.confirm_button.configure(state='disabled')
            self.preview_button.configure(state='disabled')
            self.status.configure(text=('读取候选失败 · 未授权任何归档，请检查来源后重新发现'
                if task.kind == 'discovery' else '归档预览失效 · 请重新发现后再勾选'))
            messagebox.showerror('归档剩余项目', self.owner._public(error), parent=self.window)
        elif task.kind == 'discovery':
            self._show_candidates(task, value)
        else:
            self._show_plan(value)

    def _show_candidates(self, task, discovery):
        o = self.owner
        self.discovery = discovery
        self.context = task.context
        for child in self.inner.winfo_children():
            child.destroy()
        self._clear_candidates()
        for candidate in discovery.candidates:
            rel = candidate['rel_path']
            var = tk.BooleanVar(master=self.window, value=False)
            self.variables[rel] = var
            self._candidate_traces.append((var, var.trace_add('write', self.selection_changed)))
            kind = '原本空目录' if candidate['kind'] == 'empty_directory' else '普通文件'
            ttk.Checkbutton(self.inner, text=o._public(f'{rel}（{kind}）'), variable=var).pack(anchor='w')
        self.source_label.configure(text=o._public(f'来源：{self.settings.downloads_dir}\n{o.scope_summary()}；归档到该来源内的「{self.settings.target_parent_name}/已归档」'))
        reasons = '；'.join(f'{k} {v}' for k, v in discovery.excluded_counts.items() if v)
        self.status.configure(text=(f'发现 {len(discovery.candidates)} 个可归档候选 · 默认不勾选。'
            + (f'\n未纳入：{reasons}。' if reasons else '')
            + ('\n读取失败或扫描变化，范围不完整；请重新发现，暂不能归档。' if discovery.issues else '')))
        for item in self.tree.get_children():
            self.tree.delete(item)

    def valid_context(self):
        return (not self._closed and self.context is not None and not self.owner._closing and self.context == self.fingerprint()
                and self.owner.settings is self.settings)

    def preview(self):
        if self._closed or self.owner._closing or self.owner._operation_busy or not self.selection():
            return
        if self._task and self.task_matches(self._task):
            return
        self.plan = None
        self.confirm_button.configure(state='disabled')
        if not self.valid_context():
            self.status.configure(text='来源、范围或设置已变化 · 请重新发现并勾选')
            return
        self._revision += 1
        self.preview_button.configure(state='disabled')
        self.status.configure(text='正在准备归档预览… 可关闭窗口')
        for item in self.tree.get_children():
            self.tree.delete(item)
        task = PreparationTask('preview', self.settings, self.fingerprint(), self._revision,
            {'selection': self.selection(), 'scope_dirs': list(self.discovery.selected_scope),
             'recursive': self.discovery.recursive, 'expected_candidates': copy.deepcopy(self.discovery.candidates)})
        self._task = task
        self.preparation.submit(self, task)

    def _show_plan(self, plan):
        self.plan = plan
        for item in self.tree.get_children():
            self.tree.delete(item)
        for op in plan['operations'] + plan['directory_operations']:
            self.tree.insert('', 'end', values=(self.owner._public(op['source']), self.owner._public(op['dest'])))
        self.status.configure(text=f"待归档 {len(plan['operations'])} 个文件、{len(plan['directory_operations'])} 个原本空目录 · 请确认归档")
        self.confirm_button.configure(state='normal')
        self.preview_button.configure(state='normal')

    def confirm(self):
        o = self.owner
        if self._closed or o._closing or o._operation_busy or not self.plan:
            return
        plan = self.plan
        if not self.valid_context() or self.selection() != plan['remaining_selection']:
            self.plan = None
            self.confirm_button.configure(state='disabled')
            self.status.configure(text='归档预览已失效 · 请重新发现并勾选')
            return
        o._operation_busy = True
        o._refresh_plan_buttons()
        attempted = False
        try:
            prompt = (f"归档 {len(plan['operations'])} 个文件、{len(plan['directory_operations'])} 个原本空目录。\n"
                f"来源：{self.settings.downloads_dir}\n范围：{o.scope_summary()}\n"
                f"归档根：{self.settings.target_root}/已归档/{plan['plan_id']}\n\n"
                + RISK_NOTICE + '\n不删除、绝不覆盖；可尝试撤销。继续归档？')
            if not messagebox.askokcancel('确认归档剩余项目', o._public(prompt), parent=self.window):
                self.status.configure(text='已取消归档 · 未移动文件或目录')
                return
            if self.plan is not plan or not self.valid_context() or self.selection() != plan['remaining_selection']:
                self.plan = None
                self.status.configure(text='确认期间方案已变化 · 请重新发现并预览')
                return
            write_confirmation(self.settings, plan['plan_id'], plan['plan_hash'])
            attempted = True
            o._invalidate_discovery()
            result = execute_plan(self.settings, plan['plan_id'], plan['plan_hash'], kind='gui')
            o.plan = plan
            o.confirmed_hash = None
            o._preview_ready = False
            o._chain_result = None
            categories = o._selected_categories()
            o._render_plan(plan)
            for category, var in o._selection_vars.items():
                var.set(category in categories)
            self.plan = None
            self.confirm_button.configure(state='disabled')
            self.preview_button.configure(state='disabled')
            message = f'归档文件 {result.moved} 个、目录 {result.archived_dirs} 个'
            if result.stopped or result.skipped or result.failed or result.not_attempted or result.dirs_skipped or result.dirs_failed or result.dirs_not_attempted:
                message += (f'；未全部完成：文件跳过 {result.skipped}、失败 {result.failed}、未处理 {result.not_attempted}；'
                    f'目录未满足 {result.dirs_skipped}、失败/待检查 {result.dirs_failed}、未处理 {result.dirs_not_attempted}。请查看详情／恢复检查。')
                messagebox.showwarning('归档剩余项目', o._public(message), parent=self.window)
            else:
                messagebox.showinfo('归档剩余项目', message + '。可通过主页面撤销或操作记录退回。', parent=self.window)
            self.status.configure(text=o._public(message) + ' · 再归档须重新发现')
        except Exception as exc:
            self.plan = None
            self.confirm_button.configure(state='disabled')
            if attempted:
                o.plan = plan
                o.confirmed_hash = None
                o._preview_ready = False
                o._chain_result = None
                categories = o._selected_categories()
                o._render_plan(plan)
                for category, var in o._selection_vars.items():
                    var.set(category in categories)
                o._state_problem = '剩余归档失败或需检查 · 未确认全部完成，请查看操作记录／恢复检查。'
                o._show_daily_problems()
            self.status.configure(text='归档失败或需检查 · 不代表全部完成，请查看操作记录／恢复检查')
            messagebox.showerror('归档剩余项目', o._public(exc), parent=self.window)
        finally:
            try:
                if attempted:
                    o.session.track(self.settings, plan['plan_id'])
            finally:
                o._operation_busy = False
                o._refresh_plan_buttons()
