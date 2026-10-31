"""UI-independent session controller. Core plans, journals and moves remain authoritative.

Every mutating request claims one worker slot before returning. Views are copies;
no widget, browser object or caller-owned collection participates in admission.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime
import hashlib
import os
import re
import secrets
import stat
import threading
from dataclasses import asdict

from .config import assert_downloads_exists, ensure_state_dirs
from .executor import execute_plan, recover_execution
from .gui_session import OperationSession, latest_restorable_plan
from .gui_source import selected_settings
from .journal import JournalCorrupted, JournalNeedsRepair
from .locking import read_plan_state
from .plan_hash import normalize_path
from .planner import (PlanError, assert_new_move_admissible, build_plan, list_plans,
                      load_verified_plan, plan_path, public_plan_view, write_confirmation, write_plan)
from .remaining_archive import build_remaining_plan, discover_remaining
from .rules import CATEGORY_SUBFOLDERS, decoded_filename, CATEGORIES, document_subcategory, normalize_extension, redact_private_text
from .scanner import list_selectable_folders, normalize_scope
from .snapshot import fold_verified_journals, read_snapshot
from .undo import undo_plan
from .watcher import NewFileWatcher


class RequestRefused(ValueError):
    pass


def read_history(settings, plan):
    """Same consumed-history policy as Tk's _read_plan_history; read-only."""
    if plan is None:
        return {'consumed': False, 'execution': {}, 'undo': {}, 'snapshot': None,
                'phase': 'prepared', 'state': {}, 'error': False}
    plan_id = plan['plan_id']
    state, snapshot = read_plan_state(settings, plan_id), read_snapshot(settings, plan_id)
    error = False
    try:
        if plan.get('schema_version') in (3, 4):
            from .directory_archive import read_folds
            execution, undo = read_folds(settings, plan)
        else:
            execution, undo = fold_verified_journals(settings.state_dir, plan_id)
    except (JournalCorrupted, JournalNeedsRepair, PlanError, KeyError, TypeError, ValueError, OSError):
        execution, undo, error = {}, {}, True
    phase = str(state.get('phase') or 'prepared')
    consumed = bool(error or phase != 'prepared' or state.get('counts') is not None
                    or execution or undo or (snapshot and any(
                        e.get('undo_state') != 'skipped_not_moved' for e in snapshot.get('entries', []))))
    return dict(state=state, snapshot=snapshot, phase=phase, execution=execution,
                undo=undo, error=error, consumed=consumed)


def size_text(value):
    value = int(value)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f'{value:g} {unit}'
        value = round(value / 1024, 1)


class Controller:
    def __init__(self, settings, *, clock=None):
        ensure_state_dirs(settings)
        self._base = self.settings = settings
        self._lock = threading.RLock()
        self._idle = threading.Event(); self._idle.set()
        self._closed = False
        self._thread = None
        self._revision = 0
        self._nonce = secrets.token_hex(24)
        self.selected = False
        self.phase = 'empty'
        self.busy = False
        self.categories = list(CATEGORIES)
        self.include_subfolders = False
        self.scope = []
        self.archive = False
        self.auto_check = False
        self.filter = None
        self.plan = None
        self.preview_ready = False
        self.confirmed_hash = None
        self.history = read_history(settings, None)
        self.session = OperationSession()
        self.banner = None
        self.error = ''
        self.discovery = None
        self.folders = []
        self.remaining = None
        self._remaining_discovery = None
        self._remaining_plan = None
        self._remaining_context = None
        self._remaining_selection = []
        self._remaining_revision = 0
        self._preview_conflicts = set()
        self._running_row = None
        self._can_open = False
        self._past_refs = {}
        self.watcher = NewFileWatcher(**({'clock': clock} if clock else {}))

    def _public(self, value):
        text = redact_private_text(str(value), roots=(self.settings.downloads_dir,
                                  self.settings.target_root, self.settings.state_dir))
        return re.sub(r'plan-\d{8}-\d{6}-[a-zA-Z0-9]+', '本次记录', text)

    def _public_tree(self, value):
        if isinstance(value, str):
            return self._public(value)
        if isinstance(value, dict):
            return {self._public(k): self._public_tree(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._public_tree(v) for v in value]
        return value

    def _invalidate(self):
        self._revision += 1
        self.preview_ready = False
        self.confirmed_hash = None
        self._remaining_revision += 1
        self._remaining_plan = None
        self._remaining_discovery = None
        self._remaining_context = None
        self.remaining = None

    def _require_idle(self):
        if self._closed or self.busy:
            raise RequestRefused('正在处理，请等待完成。' if self.busy else '窗口已关闭。')

    def _require_source(self):
        if not self.selected:
            raise RequestRefused('请先选择文件夹。')

    def _start(self, phase, operation):
        with self._lock:
            self._require_idle()
            self.busy = True
            previous = self.phase
            self.phase = phase
            self.error = ''
            self._idle.clear()
            def worker():
                try:
                    operation()
                except Exception as exc:
                    with self._lock:
                        self.preview_ready = False
                        self.confirmed_hash = None
                        self.error = self._public(exc)
                        self.phase = 'needs_review' if self.history.get('consumed') else 'error'
                        self.banner = {'kind': 'warn', 'title': '本次操作未完成，请先检查', 'description': self.error}
                finally:
                    with self._lock:
                        if self.phase == phase:
                            self.phase = previous
                        self._running_row = None
                        self.busy = False
                        self._idle.set()
            self._thread = threading.Thread(target=worker, name='doc-selecter-ui-operation', daemon=False)
            try:
                self._thread.start()
            except Exception:
                self.busy = False; self.phase = previous; self._idle.set()
                raise
            return {'accepted': True}

    def wait_idle(self, timeout=10):
        if not self._idle.wait(timeout):
            raise TimeoutError('等待操作完成超时')
        return self.get_state()

    def close(self):
        with self._lock:
            if self.busy and self.phase in {'organizing', 'undoing', 'recovering', 'archiving'}:
                return False
            self._closed = True
            self._invalidate()
            self.watcher.disable()
            return True

    def select_source(self, path):
        def operation():
            settings = selected_settings(self._base, path)
            with self._lock:
                if self._closed:
                    return
                self.settings = settings
                self.selected = True
                self.scope = []; self.include_subfolders = False; self.archive = False
                self.filter = None; self.plan = None; self.banner = None
                self.discovery = None; self.auto_check = False; self.watcher.disable()
                self.watcher.reset(); self._invalidate(); self._can_open = False
            def candidates():
                for plan_id in reversed(list_plans(settings)):
                    try:
                        plan = load_verified_plan(settings, plan_id, display_only=True)
                        if plan.get('downloads_dir') == settings.canonical_downloads_dir:
                            yield plan, read_history(settings, plan)
                    except (OSError, ValueError, RuntimeError):
                        continue
            restored = latest_restorable_plan(candidates())
            with self._lock:
                if self._closed:
                    return
                self.plan = restored
                self.history = read_history(settings, restored)
                self.phase = 'selected'
            if restored:
                self._refresh_history()
            else:
                self._preview()
        return self._start('scanning', operation)

    def _context(self):
        return (self.settings, tuple(self.categories), self.include_subfolders,
                tuple(self.scope), self.archive, self.auto_check)

    def _preview(self):
        with self._lock:
            self._require_source(); self._invalidate()
            revision = self._revision
            self.banner = None; self.phase = 'scanning'; self.discovery = None
            self.watcher.reset(keep_reported=True)
            settings = self.settings
            options = dict(category_selection=list(self.categories), scope_dirs=list(self.scope),
                           recursive=bool(self.include_subfolders and self.scope), archive_emptied_dirs=self.archive)
        assert_downloads_exists(settings)
        plan = build_plan(settings, **options).plan
        with self._lock:
            if self._closed or revision != self._revision:
                return
        write_plan(settings, plan)
        public = public_plan_view(plan, settings)
        # Existing target evidence is only a preview hint. Core execution rechecks it.
        conflicts = {op['op_id'] for op in public.get('operations', []) if os.path.lexists(op['dest'])}
        with self._lock:
            if self._closed or revision != self._revision:
                return
            self.plan = plan; self.preview_ready = True; self.confirmed_hash = None
            self.history = read_history(settings, plan)
            self._preview_conflicts = conflicts
            self.phase = 'preview'
            excluded = plan.get('excluded', {})
            if any(excluded.get(k, {}).get('count', 0) for k in ('元数据读取失败', '递归读取失败/扫描变化')):
                self.banner = {'kind': 'warn', 'title': '预览可能不完整',
                               'description': '有读取失败或扫描变化，请查看详情并重新生成预览。'}

    def preview(self):
        return self._start('scanning', self._preview)

    def set_categories(self, values):
        with self._lock:
            self._require_idle(); self._require_source()
            if not isinstance(values, list) or any(v not in CATEGORIES for v in values) or len(set(values)) != len(values):
                raise RequestRefused('类别选择无效。')
            self.categories = [c for c in CATEGORIES if c in values]
            self._invalidate()
            return self._start('scanning', self._preview)

    def set_filter(self, category=None):
        with self._lock:
            self._require_idle()
            if category is not None and category not in (*CATEGORIES, 'hints'):
                raise RequestRefused('类别无效。')
            self.filter = None if category == 'hints' and self.filter == 'hints' else category
            return self.get_state()

    def list_folders(self):
        def operation():
            self._require_source()
            folders = list_selectable_folders(self.settings.downloads_dir, target_parent_name=self.settings.target_parent_name)
            with self._lock:
                self.folders = [asdict(f) for f in folders]
        return self._start('discovering', operation)

    def set_subfolders(self, enabled, scope):
        with self._lock:
            self._require_idle(); self._require_source()
            if type(enabled) is not bool or not isinstance(scope, list) or any(not isinstance(p, str) for p in scope):
                raise RequestRefused('子文件夹范围无效。')
            allowed = {f['rel_path'] for f in self.folders}
            if any(p not in allowed for p in scope) or len(set(scope)) != len(scope):
                raise RequestRefused('请重新读取子文件夹，再明确选择范围。')
            # Keep explicitly selected roots; traversal ancestors never become authorization.
            self.scope = sorted(scope) if enabled else []
            self.include_subfolders = enabled
            self._invalidate()
            return self._start('scanning', self._preview)

    def settings_set(self, archive, auto_check):
        with self._lock:
            self._require_idle(); self._require_source()
            if type(archive) is not bool or type(auto_check) is not bool:
                raise RequestRefused('设置值无效。')
            changed = archive != self.archive
            self.archive, self.auto_check = archive, auto_check
            if changed:
                self._invalidate()
            else:
                self._remaining_revision += 1
                self._remaining_plan = None
                self._remaining_discovery = None
                self.remaining = None
            self.watcher.enable() if auto_check else self.watcher.disable()
            if changed:
                self.phase = 'selected'
                self.banner = {'kind': 'info', 'title': '归档设置已变化', 'description': '请重新生成预览，再确认整理。'}
            return self.get_state()

    def _valid_preview(self, expected):
        plan = self.plan
        if not (self.selected and self.preview_ready and isinstance(expected, str) and plan
                and plan.get('plan_type') != 'remaining_archive' and plan['plan_hash'] == expected):
            raise RequestRefused('方案已失效，请重新生成预览后确认。')
        if read_history(self.settings, plan)['consumed']:
            raise RequestRefused('这份方案已整理或需检查，请重新生成预览。')
        recursive = bool(self.include_subfolders and self.scope)
        if not (plan['recursive'] is recursive
                and plan['selected_scope'] == normalize_scope(self.scope if recursive else [])
                and plan['downloads_dir'] == normalize_path(self.settings.downloads_dir)
                and plan['target_root'] == normalize_path(self.settings.target_root)
                and plan['category_selection'] == self.categories
                and plan.get('archive', {}).get('enabled', False) == self.archive):
            raise RequestRefused('范围、类别或设置已变化，请重新预览。')
        assert_new_move_admissible(self.settings, plan)
        saved = load_verified_plan(self.settings, plan['plan_id'], for_new_move=True)
        if saved['plan_hash'] != expected:
            raise RequestRefused('保存的方案已变化，请重新预览。')
        return plan

    def organize(self, plan_hash, confirmed=False):
        with self._lock:
            self._require_idle()
            if confirmed is not True:
                raise RequestRefused('请先明确确认整理。')
            # Reject stale UI requests before starting a worker or saving confirmation.
            if not self.preview_ready or not self.plan or plan_hash != self.plan.get('plan_hash'):
                raise RequestRefused('方案已失效，请重新生成预览后确认。')
            def operation():
                plan = self._valid_preview(plan_hash)
                self._execute(plan)
            return self._start('organizing', operation)

    def _execute(self, plan):
        settings = self.settings
        write_confirmation(settings, plan['plan_id'], plan['plan_hash'])
        with self._lock:
            self.confirmed_hash = plan['plan_hash']; self.preview_ready = False
            self.plan = plan; self._revision += 1
        stop = threading.Event()
        observer = self._observe_results(settings, plan, stop)
        try:
            result = execute_plan(settings, plan['plan_id'], plan['plan_hash'], kind='gui', hook=self._before_move)
            with self._lock:
                self.error = self._public(result.stop_reason) if result.stopped else ''
        finally:
            stop.set()
            self.session.track(settings, plan['plan_id'])
            self._refresh_history()
            self.confirmed_hash = None

    def _before_move(self, event, operation):
        if event in {'before_move', 'before_undo_move'}:
            with self._lock:
                self._running_row = operation['op_id']

    def _observe_results(self, settings, plan, stop):
        def observe():
            while not stop.wait(.08):
                try:
                    history = read_history(settings, plan)
                    if not history['error']:
                        with self._lock:
                            if self.plan is plan and not self._closed and not stop.is_set():
                                self.history = history
                except (OSError, ValueError, RuntimeError):
                    pass  # Partial writes are not proof of a completed row.
        thread = threading.Thread(target=observe, name='doc-selecter-ui-results', daemon=True)
        thread.start()
        return thread

    def _undo_allowed(self):
        if self.session.active:
            step = self.session.active[-1]
            if self.history['phase'] in {'corrupted', 'undoing'}:
                return False
            if self.history['phase'] in {'needs_review', 'unknown_needs_review', 'aborted_global', 'aborted_log_failure', 'executing', 'locked'} and not step.checked:
                return False
            return not step.uncertain and (step.checked or step.status == 'active')
        h = self.history
        return bool(self.plan and not h['error'] and h['phase'] not in {
            'undoing', 'corrupted', 'needs_review', 'unknown_needs_review',
            'aborted_log_failure', 'aborted_global', 'executing', 'locked'}
            and any(e.get('undo_state') in {'pending', 'attempting'}
                    for e in (h['snapshot'] or {}).get('entries', [])
                    + (h['snapshot'] or {}).get('directory_entries', [])))

    def _token(self, version):
        data = repr((self._nonce, self._revision, version, self.session.head,
                     [(s.version, s.plan_id, s.status, s.checked) for s in self.session.active],
                     (self.plan or {}).get('plan_hash')))
        return hashlib.sha256(data.encode()).hexdigest()

    def undo(self, token, confirmed=False):
        with self._lock:
            self._require_idle()
            version = self.session.active[-1].parent if self.session.active else None
            if confirmed is not True or token != self._token(version) or not self._undo_allowed():
                raise RequestRefused('撤销条件已变化，请重新查看并确认；需要时先恢复检查。')
            return self._start('undoing', lambda: self._return(version))

    def return_to(self, version, token, confirmed=False):
        with self._lock:
            self._require_idle()
            if type(version) is not int or confirmed is not True or token != self._token(version):
                raise RequestRefused('操作记录已变化，请重新查看并确认。')
            self.session.batches_after(version)
            return self._start('undoing', lambda: self._return(version))

    def _return(self, version):
        self._revision += 1
        self.preview_ready = False
        self.confirmed_hash = None
        self.banner = None
        restored = directories = 0
        reached = False
        try:
            if version is None:
                result = self._undo_batch(self.settings, self.plan['plan_id'], confirm=True, kind='gui')
                restored, directories = result.restored, result.restored_dirs
                reached = result.undo_status == 'completed'
            else:
                report = self.session.return_to(version, self._undo_batch)
                restored, directories, reached = report.restored, report.restored_dirs, report.reached
                if not reached:
                    self.error = self._public(report.detail)
        finally:
            self._refresh_history()
        with self._lock:
            if reached and self.phase == 'undone':
                title = f'已还原 {restored} 个文件到原位置'
                if directories:
                    title += f'、{directories} 个原目录'
                self.banner = {'kind': 'info', 'title': title, 'description': '可以调整类别后重新生成预览。'}
            else:
                self.phase = 'needs_review'
                self.banner = {'kind': 'warn', 'title': f'撤销未全部完成 · 已还原 {restored} 个文件',
                               'description': self.error or '原位置被占用或文件已变化；更早的操作没有撤销，请查看详情并检查。'}

    def _undo_batch(self, settings, plan_id, **kwargs):
        plan = load_verified_plan(settings, plan_id, display_only=True)
        with self._lock:
            self.settings = settings; self.plan = plan
            self.scope = [p for p in plan.get('selected_scope', []) if p]
            self.include_subfolders = bool(plan.get('recursive'))
            self.filter = None
            self.archive = bool(plan.get('archive', {}).get('enabled'))
        stop = threading.Event()
        self._observe_results(settings, plan, stop)
        try:
            return undo_plan(settings, plan_id, hook=self._before_move, **kwargs)
        finally:
            stop.set()

    def recover_check(self, token, version=None):
        with self._lock:
            self._require_idle()
            if token != self._token(version):
                raise RequestRefused('记录已变化，请重新查看后检查。')
            if version is None:
                if self.plan is None:
                    raise RequestRefused('没有可检查的记录。')
                settings, plan_id = self.settings, self.plan['plan_id']
            else:
                step = next((s for s in self.session.steps if s.version == version), None)
                if step is None:
                    raise RequestRefused('操作记录不存在。')
                settings, plan_id = step.settings, step.plan_id
            def operation():
                recover_execution(settings, plan_id, kind='gui')
                self.session.checked_batch(settings, plan_id)
                self._revision += 1
                if self.plan and self.plan['plan_id'] == plan_id:
                    self._refresh_history()
            return self._start('recovering', operation)

    def _refresh_history(self):
        history = read_history(self.settings, self.plan)
        snapshot = history['snapshot'] or {}
        entries = snapshot.get('entries', []) + snapshot.get('directory_entries', [])
        counts = history['state'].get('counts') or {}
        complete = bool(history['phase'] == 'undone' and snapshot.get('undo_status') == 'completed'
                        and entries and not history['error']
                        and all(e.get('undo_state') in {'restored', 'skipped_not_moved'} for e in entries))
        problems = (history['error'] or not snapshot or any(
            e.get('undo_state') not in {'pending', 'attempting', 'restored', 'skipped_not_moved'} for e in entries)
            or history['phase'] in {'corrupted', 'needs_review', 'unknown_needs_review', 'aborted_log_failure',
                                   'aborted_global', 'locked', 'executing', 'undoing', 'partial_undo'}
            or any(counts.get(k, 0) for k in ('failed', 'not_attempted', 'dirs_failed', 'dirs_not_attempted')))
        try:
            can_open = history['phase'] == 'executed' and stat.S_ISDIR(os.lstat(self.settings.target_root).st_mode)
        except OSError:
            can_open = False
        with self._lock:
            self.history = history; self._can_open = can_open
            self.preview_ready = False; self.confirmed_hash = None
            if complete:
                self.phase = 'undone'
                restored = sum(e.get('undo_state') == 'restored' for e in snapshot.get('entries', []))
                self.banner = {'kind': 'info', 'title': f'已还原 {restored} 个文件到原位置',
                               'description': '可以调整类别后重新生成预览。'}
            elif problems or history['phase'] != 'executed':
                self.phase = 'needs_review'
                self.banner = {'kind': 'warn', 'title': '操作未全部完成，请先检查',
                               'description': self.error or '有文件未处理、还原受阻或记录需要检查；请查看详情和操作记录。'}
            else:
                self.phase = 'done'
                moved, skipped = counts.get('moved', 0), counts.get('skipped', 0)
                desc = f'{skipped} 个文件已跳过，原文件留在原处；请查看对应原因。' if skipped else '文件已放到对应文件夹，之后可以撤销。'
                results = [(v.get('result') or {}) for v in history['execution'].values()]
                skipped_results = [r for r in results if r.get('outcome') == 'skipped']
                if skipped_results and all(r.get('reason_code') == 'DEST_EXISTS' or '目标已存在' in r.get('reason', '') for r in skipped_results):
                    desc = f'{skipped} 个同名文件已存在，没有覆盖，原文件留在原处。'
                title = f'已整理 {moved} 个文件'
                if self.plan.get('plan_type') == 'remaining_archive':
                    title = f'已归档 {moved} 个文件、{counts.get("archived_dirs", 0)} 个原本空目录'
                self.banner = {'kind': 'ok', 'title': title, 'description': desc}

    def check_new_files(self):
        def operation():
            self._require_source()
            watcher = copy.copy(self.watcher)
            watcher._tracked = copy.deepcopy(self.watcher._tracked)
            watcher._reported = set(self.watcher._reported)
            watcher.enable()
            report = watcher.check_once(downloads_dir=self.settings.downloads_dir,
                target_parent_name=self.settings.target_parent_name, scope_dirs=list(self.scope),
                recursive=bool(self.include_subfolders and self.scope),
                extra_ignored={op.get('rel_path', '') for op in (self.plan or {}).get('operations', [])})
            with self._lock:
                if self._closed:
                    return
                if not self.auto_check:
                    watcher._disabled = True  # Keep settling observations for the next manual check.
                self.watcher = watcher
                self.discovery = asdict(report)
                if report.error or report.partial_coverage:
                    self.banner = {'kind': 'warn', 'title': '新文件检查不完整',
                                   'description': '有读取失败或扫描变化，请查看详情并重新检查。'}
                elif report.new_files:
                    self.banner = {'kind': 'info', 'title': f'发现 {len(report.new_files)} 个新文件',
                                   'description': '只做提示；重新生成预览后再决定是否整理。'}
        return self._start('checking', operation)

    def remaining_discover(self):
        with self._lock:
            self._require_idle()
            self._remaining_revision += 1
            revision = self._remaining_revision
        def operation():
            self._require_source()
            discovery = discover_remaining(self.settings, scope_dirs=list(self.scope),
                                           recursive=bool(self.include_subfolders and self.scope))
            with self._lock:
                if self._closed:
                    return
                if revision != self._remaining_revision:
                    return
                self._remaining_discovery = discovery
                self._remaining_context = self._context()
                self._remaining_plan = None; self._remaining_selection = []
                self._remaining_revision += 1
                self.remaining = {'candidates': copy.deepcopy(discovery.candidates), 'selected': [],
                                  'excluded': discovery.excluded_counts, 'issues': discovery.issues,
                                  'rows': [], 'plan_hash': None, 'status': '请逐项勾选，默认不会归档任何项目。'}
        return self._start('discovering', operation)

    def remaining_select(self, selection):
        with self._lock:
            self._require_idle()
            d = self._remaining_discovery
            if d is None or self._remaining_context != self._context():
                raise RequestRefused('范围或设置已变化，请重新发现。')
            if not isinstance(selection, list) or any(not isinstance(s, str) for s in selection):
                raise RequestRefused('归档选择无效。')
            allowed = {c['rel_path'] for c in d.candidates}
            if len(set(selection)) != len(selection) or any(s not in allowed for s in selection):
                raise RequestRefused('请选择当前列出的可归档项目。')
            self._remaining_revision += 1; self._remaining_plan = None
            self._remaining_selection = sorted(selection)
            self.remaining.update(selected=list(self._remaining_selection), plan_hash=None, rows=[],
                                  status='勾选已变化，请生成归档预览。')
            return self.get_state()

    def remaining_preview(self):
        def operation():
            d = self._remaining_discovery
            if not d or d.issues or not self._remaining_selection or self._remaining_context != self._context():
                raise RequestRefused('请重新发现并明确勾选可归档项目。')
            revision = self._remaining_revision
            plan = build_remaining_plan(self.settings, selection=list(self._remaining_selection),
                scope_dirs=list(d.selected_scope), recursive=d.recursive,
                expected_candidates=copy.deepcopy(d.candidates)).plan
            with self._lock:
                if self._closed or revision != self._remaining_revision:
                    return
            write_plan(self.settings, plan)
            with self._lock:
                if self._closed or revision != self._remaining_revision:
                    return
                self._remaining_plan = plan
                self.remaining.update(plan_hash=plan['plan_hash'], rows=self._rows(plan, read_history(self.settings, None)),
                                      status='请核对归档去向，再明确确认。')
        return self._start('preparing', operation)

    def remaining_cancel(self):
        with self._lock:
            if self.busy and self.phase == 'archiving':
                raise RequestRefused('正在归档，请等待完成。')
            self._remaining_revision += 1; self._remaining_plan = None
            self._remaining_discovery = None; self._remaining_context = None
            self._remaining_selection = []; self.remaining = None
            return {'accepted': True}

    def remaining_organize(self, plan_hash, confirmed=False):
        with self._lock:
            self._require_idle()
            plan = self._remaining_plan
            if (confirmed is not True or not plan or plan['plan_hash'] != plan_hash
                    or self._remaining_context != self._context()
                    or plan['remaining_selection'] != self._remaining_selection):
                raise RequestRefused('归档预览已失效，请重新发现并明确确认。')
            def operation():
                saved = load_verified_plan(self.settings, plan['plan_id'], for_new_move=True)
                if saved['plan_hash'] != plan_hash or read_history(self.settings, plan)['consumed']:
                    raise RequestRefused('这份归档方案已变化或已使用。')
                self._remaining_plan = None
                self._execute(plan)
                self.remaining = None
            return self._start('archiving', operation)

    def target_to_open(self):
        with self._lock:
            self._require_idle()
            if not self.selected or self.history['phase'] != 'executed':
                raise RequestRefused('整理完成后才可打开目标文件夹。')
            try:
                valid = stat.S_ISDIR(os.lstat(self.settings.target_root).st_mode)
            except OSError:
                valid = False
            if not valid:
                raise RequestRefused('目标目录不存在或是链接，未打开。')
            return self.settings.target_root

    def _rows(self, plan, history, settings=None):
        if plan is None:
            return []
        settings = settings or self.settings
        public = public_plan_view(plan, settings)
        snap = {e['op_id']: e for e in (history.get('snapshot') or {}).get('entries', [])
                + (history.get('snapshot') or {}).get('directory_entries', [])}
        rows = []
        by_path = {op.get('rel_path'): op for op in public.get('operations', [])}
        preview_time = self._plan_time(plan)
        for op in public.get('operations', []) + public.get('directory_operations', []):
            op_id = op['op_id']; result = history.get('execution', {}).get(op_id, {}).get('result') or {}
            outcome = result.get('outcome')
            undo = snap.get(op_id, {}).get('undo_state')
            state, label = '', ''
            reason = self._public(result.get('reason', ''))
            if undo == 'restored':
                state, label = 'back', '已放回'
            elif undo and (undo.startswith('blocked_') or undo == 'unknown_needs_review'):
                state, label = 'warn', '还原受阻 · 请检查'
            elif outcome in {'moved', 'archived'}:
                state, label = 'ok', '已移动' if outcome == 'moved' else '已归档'
            elif outcome == 'skipped':
                state, label = 'sk', '已跳过'
                if '目标已存在' in reason or result.get('reason_code') == 'DEST_EXISTS':
                    label += ' · 同名已存在'; reason = '同名文件已存在，未覆盖'
                else:
                    label += ' · ' + reason
            elif outcome:
                state, label = 'warn', '未完成 · 请检查'
            elif self.busy and self._running_row == op_id:
                state, label = 'run', '正在放回' if self.phase == 'undoing' else '整理中'
            elif plan is self.plan and op_id in self._preview_conflicts and self.preview_ready:
                state, label = 'hint', '会跳过 · 同名已存在'
            name = op.get('rel_path') or os.path.basename(op['source'])
            display_name = os.path.join(os.path.dirname(name), decoded_filename(os.path.basename(name)))
            hints = []
            duplicate = re.fullmatch(r"(.+) \([0-9]+\)(\.[^/]+)", os.path.basename(name))
            if duplicate:
                original = duplicate.group(1) + duplicate.group(2)
                other = by_path.get(os.path.join(os.path.dirname(name), original))
                size = op.get('identity', {}).get('size_bytes')
                if other and type(size) is int and size == other.get('identity', {}).get('size_bytes'):
                    hints.append({'kind': 'duplicate', 'text': f'可能和「{decoded_filename(original)}」是同一个文件（大小一样）'})
            mtime = op.get('identity', {}).get('mtime_ns')
            if op.get('category') == '安装包' and preview_time is not None and type(mtime) is int:
                days = (int(preview_time) * 1_000_000_000 - mtime) // 86_400_000_000_000
                if days >= 30:
                    hints.append({'kind': 'old_installer', 'text': f'放了 {days} 天，可能已经装过'})
            if display_name != name:
                hints.append({'kind': 'encoded', 'text': '名字是网页编码，这里显示成中文；文件本身的名字不变'})
            rows.append({'id': op_id, 'name': name, 'display_name': display_name, 'hints': hints,
                'subfolder': op.get('subfolder', op.get('document_subcategory')), 'category': op.get('category', '原本空目录'),
                'source': self._public(op['source']), 'destination': self._public(op['dest']),
                'to': self._public(os.path.dirname(os.path.relpath(op['dest'], settings.downloads_dir))),
                'size': size_text(op.get('identity', {}).get('size_bytes', 0)),
                'format': document_subcategory(normalize_extension(name)) or normalize_extension(name).lstrip('.').upper() or '?',
                'basis': self._public(op.get('classification_basis', '原本空目录')),
                'state': state, 'label': label, 'reason': reason})
        return rows

    @staticmethod
    def _plan_time(plan):
        try:
            return datetime.datetime.strptime(plan['created_at'], '%Y-%m-%dT%H:%M:%S%z').timestamp()
        except (KeyError, TypeError, ValueError):
            return None

    def _step_time(self, step):
        try:
            return self._plan_time(load_verified_plan(step.settings, step.plan_id, display_only=True))
        except (OSError, ValueError, RuntimeError):
            return None

    def _past_settings(self, plan):
        # Display only: earlier runs may belong to another source folder.
        return dataclasses.replace(self._base, downloads_dir=plan['downloads_dir'])

    def _past_runs(self):
        """Organized plans from earlier launches, newest first. Read-only: no undo credentials."""
        current = {s.plan_id for s in self.session.steps}
        here = self.settings.canonical_downloads_dir if self.selected else None
        runs, self._past_refs = [], {}
        for plan_id in reversed(list_plans(self._base)):
            if plan_id in current:
                continue
            try:
                plan = load_verified_plan(self._base, plan_id, display_only=True)
                settings = self._past_settings(plan)
                history = read_history(settings, plan)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                continue
            if not history['consumed']:
                continue
            counts = history['state'].get('counts') or {}
            outcomes = [(v.get('result') or {}).get('outcome') for v in history['execution'].values()]
            moved = max(int(counts.get('moved', 0)), outcomes.count('moved'))
            failed = int(counts.get('failed', 0))
            if history['phase'] == 'undone':
                status = 'undone'
            elif history['error'] or history['phase'] in {'corrupted', 'needs_review', 'unknown_needs_review',
                    'aborted_log_failure', 'aborted_global', 'executing', 'undoing', 'locked'}:
                status = 'check'
            else:
                status = 'partial' if failed else 'kept'
            try:
                written = os.stat(plan_path(self._base, plan_id)).st_mtime_ns
            except OSError:
                written = 0
            ref = hashlib.sha256((self._nonce + plan_id).encode()).hexdigest()[:24]
            self._past_refs[ref] = plan_id
            runs.append({'ref': ref, 'at': self._plan_time(plan), 'status': status,
                'kind': 'archive' if plan.get('plan_type') == 'remaining_archive' else 'organize',
                'source': self._public(os.path.basename(plan['downloads_dir'].rstrip('/'))),
                'here': plan['downloads_dir'] == here, 'moved': moved,
                'archived': outcomes.count('archived'), 'skipped': int(counts.get('skipped', 0)),
                'failed': failed, '_order': (self._plan_time(plan) or 0, written)})
        # Plan ids only resolve to the second; the plan file write time breaks ties.
        runs.sort(key=lambda r: r.pop('_order'), reverse=True)
        return runs

    def get_history(self, version=None):
        with self._lock:
            if isinstance(version, str):
                plan_id = self._past_refs.get(version)
                if plan_id is None:
                    raise RequestRefused('操作记录不存在。')
                plan = load_verified_plan(self._base, plan_id, display_only=True)
                settings = self._past_settings(plan)
                history = read_history(settings, plan)
                return self._public_tree({'rows': self._rows(plan, history, settings),
                    'at': self._plan_time(plan), 'source': os.path.basename(plan['downloads_dir'].rstrip('/')),
                    'execution': history['execution'], 'undo': history['undo']})
            if version is not None:
                if type(version) is not int:
                    raise RequestRefused('操作记录不存在。')
                step = next((s for s in self.session.steps if s.version == version), None)
                if step is None:
                    raise RequestRefused('操作记录不存在。')
                plan = load_verified_plan(step.settings, step.plan_id, display_only=True)
                history = read_history(step.settings, plan)
                return self._public_tree({'version': version,
                    'rows': self._rows(plan, history, step.settings),
                    'execution': history['execution'], 'undo': history['undo'],
                    'snapshot': history['snapshot']})
            steps = []
            for step in self.session.steps:
                steps.append({'version': step.version, 'parent': step.parent, 'status': step.status,
                    'source': self._public(step.settings.downloads_dir), 'moved': step.moved,
                    'archived_dirs': step.archived_dirs, 'skipped': step.skipped, 'failed': step.failed,
                    'uncertain': step.uncertain, 'detail': self._public(step.detail),
                    'active': step in self.session.active, 'checked': step.checked, 'at': self._step_time(step),
                    'token': self._token(step.version)})
            return {'steps': steps, 'past': self._past_runs(), 'head': self.session.head, 'root_token': self._token(0),
                    'current_token': self._token(None)}

    def get_details(self):
        with self._lock:
            public = public_plan_view(self.plan, self.settings) if self.plan else {}
            return self._public_tree({'rows': self._rows(self.plan, self.history),
                'excluded': public.get('excluded', {}), 'counts': self.history['state'].get('counts', {}),
                'snapshot': self.history.get('snapshot'), 'execution': self.history.get('execution'),
                'undo': self.history.get('undo'), 'directories': public.get('directory_operations', []),
                'discovery': self.discovery, 'error': self.error})

    def get_state(self):
        with self._lock:
            rows = self._rows(self.plan, self.history)
            shown = [r for r in rows if self.filter is None or (bool(r['hints']) if self.filter == 'hints' else r['category'] == self.filter)]
            counts = (self.plan or {}).get('category_totals', {}).get('counts', {})
            done = self.phase == 'done'
            count_rows = {c: {'count': sum(r['category'] == c and r['state'] == 'ok' for r in rows) if done else counts.get(c, 0),
                             'skipped': sum(r['category'] == c and r['state'] == 'sk' for r in rows) if done else 0,
                             'selected': c in self.categories, 'will_skip': sum(r['category'] == c and r['state'] == 'hint' for r in rows)} for c in CATEGORIES}
            collisions = len(self._preview_conflicts) if self.preview_ready else sum(r['state'] == 'sk' for r in rows)
            can_organize = self.preview_ready and not self.history['consumed'] and bool(rows) and not self.busy
            version = self.session.active[-1].parent if self.session.active else None
            return copy.deepcopy({'phase': self.phase, 'busy': self.busy, 'selected': self.selected,
                'revision': self._revision, 'folder': self._public(os.path.basename(self.settings.downloads_dir)) if self.selected else '',
                'categories': count_rows, 'category_subfolders': CATEGORY_SUBFOLDERS,
                'hint_count': sum(bool(r['hints']) for r in rows), 'filter': self.filter, 'rows': shown,
                'moved_count': sum(r['state'] == 'ok' for r in rows), 'total': sum(counts.values()), 'selected_count': len(rows), 'movable_count': len(rows) - collisions,
                'skipped_count': collisions, 'excluded_count': (self.plan or {}).get('counts', {}).get('excluded', 0), 'plan_hash': (self.plan or {}).get('plan_hash'),
                'banner': self.banner, 'error': self.error, 'can_organize': bool(can_organize),
                'can_undo': not self.busy and self._undo_allowed(), 'undo_token': self._token(version),
                'can_open': self._can_open and self.phase == 'done' and not self.busy,
                'settings': {'archive': self.archive, 'auto_check': self.auto_check},
                'subfolders': {'enabled': self.include_subfolders, 'selected': self.scope, 'candidates': self.folders},
                'remaining': self.remaining, 'discovery': self.discovery})
