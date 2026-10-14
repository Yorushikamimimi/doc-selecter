"""本次打开期间的操作分支；逐批调用原撤销入口，不持有跨批嵌套锁。"""
from dataclasses import dataclass, field
from typing import Callable

from .config import Settings
from .executor import classify_journals
from .locking import read_plan_state
from .snapshot import fold_verified_journals, read_snapshot


@dataclass
class Step:
    version: int
    parent: int
    settings: Settings
    plan_id: str
    moved: int
    uncertain: int
    status: str = 'active'
    detail: str = ''
    skipped: int = 0
    failed: int = 0
    not_attempted: int = 0
    checked: bool = False
    archived_dirs: int = 0
    uncertain_dirs: int = 0


@dataclass
class ReturnReport:
    target: int
    completed: list[int] = field(default_factory=list)
    restored: int = 0
    stopped_at: int | None = None
    detail: str = ''
    reached: bool = False
    restored_dirs: int = 0
    exception: bool = False


class OperationSession:
    def __init__(self):
        self.steps: list[Step] = []
        self.active: list[Step] = []
        self.busy = False

    @property
    def head(self) -> int:
        return self.active[-1].version if self.active else 0

    def track(self, settings: Settings, plan_id: str) -> Step | None:
        if any(s.plan_id == plan_id and s.settings.state_dir == settings.state_dir for s in self.steps):
            return None
        state = read_plan_state(settings, plan_id)
        counts = state.get('counts') or {}
        moved = int(counts.get('moved', 0))
        uncertain = 0
        archived_dirs = uncertain_dirs = 0
        try:
            execution, _ = fold_verified_journals(settings.state_dir, plan_id)
            archived_dirs = sum((s.get('result') or {}).get('outcome') == 'archived' for s in execution.values())
            uncertain_dirs = sum(self._uncertain(s) and (s.get('intent') or {}).get('op_kind') == 'empty_directory' for s in execution.values())
            moved = max(moved, sum((s.get('result') or {}).get('outcome') == 'moved'
                                   for s in execution.values()))
            uncertain = sum(self._uncertain(s) for s in execution.values())
        except (OSError, RuntimeError, ValueError):
            # 损坏/不可判的持久化批次保守跟踪，绝不以异常证明零移动。
            uncertain = 1
        if not moved and not uncertain and not archived_dirs:
            return None
        step = Step(len(self.steps) + 1, self.head, settings, plan_id, moved, uncertain)
        step.archived_dirs = archived_dirs
        step.uncertain_dirs = uncertain_dirs
        step.skipped = int(counts.get('skipped', 0))
        step.failed = int(counts.get('failed', 0))
        step.not_attempted = int(counts.get('not_attempted', 0))
        if uncertain:
            step.status = 'needs_review'
        self.steps.append(step)
        self.active.append(step)
        return step

    @staticmethod
    def _uncertain(slot) -> bool:
        result = slot.get('result') or {}
        return bool(slot.get('intent')) and (
            result.get('outcome') not in {'moved', 'archived', 'created', 'not_created', 'skipped', 'failed'}
            or result.get('reason_code') in {'MOVE_IDENTITY_MISMATCH', 'UNKNOWN_ERROR'})

    def checked_batch(self, settings: Settings, plan_id: str) -> None:
        """只刷新会话展示；恢复由原入口完成，不重写阶段或身份基准。"""
        step = next((s for s in self.active if s.plan_id == plan_id
                     and s.settings.state_dir == settings.state_dir), None)
        if step is None:
            return
        step.checked = False
        try:
            execution, _ = fold_verified_journals(settings.state_dir, plan_id)
            step.uncertain = sum(self._uncertain(s) for s in execution.values())
            step.archived_dirs = sum((s.get('result') or {}).get('outcome') == 'archived' for s in execution.values())
            step.uncertain_dirs = sum(self._uncertain(s) and (s.get('intent') or {}).get('op_kind') == 'empty_directory' for s in execution.values())
            step.moved = sum((s.get('result') or {}).get('outcome') == 'moved' for s in execution.values())
            snap = read_snapshot(settings, plan_id) or {}
            step.checked = (not step.uncertain and bool(snap.get('entries'))
                            and all(e.get('undo_state') in {'pending', 'restored', 'skipped_not_moved'}
                                    for e in snap['entries'] + snap.get('directory_entries', []))
                            and all(v['state'].healthy for v in classify_journals(settings, plan_id).values()))
        except (OSError, RuntimeError, ValueError):
            step.status = 'needs_review'
            return
        if step.checked:
            step.status = 'active'

    def batches_after(self, version: int) -> list[Step]:
        chain = [0] + [s.version for s in self.active]
        if version not in chain:
            raise ValueError('该版本属于已撤销的分支，不能作为当前退回目标。')
        return list(reversed(self.active[chain.index(version):]))

    def return_to(self, version: int, undo: Callable) -> ReturnReport:
        if self.busy:
            raise RuntimeError('正在撤销，请等待完成。')
        batches = self.batches_after(version)
        report = ReturnReport(version)
        self.busy = True
        try:
            for step in batches:
                try:
                    phase = read_plan_state(step.settings, step.plan_id).get('phase')
                    if (phase in {'corrupted', 'undoing'}
                            or (phase in {'needs_review', 'unknown_needs_review', 'aborted_global',
                                          'aborted_log_failure', 'executing', 'locked'} and not step.checked)
                            or step.uncertain):
                        raise RuntimeError('本批记录需先恢复/检查；更早步骤未撤销。')
                    result = undo(step.settings, step.plan_id, confirm=True, kind='gui')
                    report.restored += result.restored
                    report.restored_dirs += getattr(result, "restored_dirs", 0)
                    state = read_plan_state(step.settings, step.plan_id)
                    snap = read_snapshot(step.settings, step.plan_id) or {}
                    healthy = all(v['state'].healthy for v in classify_journals(step.settings, step.plan_id).values())
                    complete = (result.undo_status == 'completed' and state.get('phase') == 'undone'
                                and snap.get('undo_status') == 'completed' and healthy
                                and bool(snap.get('entries')) and all(
                                    e.get('undo_state') in {'restored', 'skipped_not_moved'}
                                    for e in snap['entries'] + snap.get('directory_entries', [])))
                    if not complete:
                        step.status = 'needs_review'
                        report.stopped_at = step.version
                        report.detail = '本批未完整还原；更早步骤未撤销，请查看详情并检查。'
                        break
                    step.status = 'undone'
                    self.active.pop()
                    report.completed.append(step.version)
                except (OSError, RuntimeError, ValueError) as exc:
                    report.exception = True
                    step.status = 'needs_review'
                    step.detail = str(exc)
                    report.stopped_at = step.version
                    report.detail = str(exc)
                    break
            report.reached = report.stopped_at is None and self.head == version
            return report
        finally:
            self.busy = False
