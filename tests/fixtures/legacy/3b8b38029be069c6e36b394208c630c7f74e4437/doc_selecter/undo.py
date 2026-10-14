"""撤销：身份检查 + 逐项日志 + 中断恢复（docs/MVP方案.md §5.4 / §5.4.1 / §7.3）。

v3.1 收口要点：

1. **分流**：只有 ``attempting``（已持久化 ``undo_intent``、尚无 ``undo_result``）的条目
   进入 5.4.1 中断判定；普通 ``pending`` 走正常流程（**不得**整批判为未知）。
2. **续作复用**：``attempting`` 条目续作时复用已有 ``undo_intent``，**不追加第二条**。
3. **预检跳过**：确认撤销后预检即被阻止的项写 ``undo_skip``（**不产生孤立 result**）。
4. **身份核对**：``restored`` 必须五项身份匹配；仅凭存在性不得判还原。
5. **失败范围**：local 冲突跳过并继续；global 问题停止整批。
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .atomic_move import (
    AtomicMover,
    AtomicMoveUnavailable,
    DestinationExists,
    DirectoryIdentityChanged,
    FileIdentity,
    MoveFailed,
    OutOfScopeError,
    assert_no_symlink_segment_for_file,
    dir_identity,
    fd_identity,
    lstat_identity,
    open_dir_fd,
)
from .config import Settings
from .executor import (
    ExecutionRefused,
    assert_journals_admissible,
    classify_journals,
    probe_capability,
)
from .journal import (
    Journal,
    JournalCorrupted,
    JournalNeedsRepair,
    JournalWriteFailed,
    ReadReport,
    make_event,
)
from .locking import (
    GlobalLock,
    LockBusy,
    PHASE_ABORTED_GLOBAL,
    PHASE_ABORTED_LOG_FAILURE,
    PHASE_CORRUPTED,
    PHASE_NEEDS_REVIEW,
    PHASE_PARTIAL_UNDO,
    PHASE_UNDONE,
    PHASE_UNDOING,
    current_phase,
    read_plan_state,
    write_plan_state,
)
from .planner import PlanError, load_verified_plan
from .snapshot import (
    GLOBAL_UNDO_STATES,
    LOCAL_UNDO_STATES,
    TERMINAL_UNDO_STATES,
    UNDO_ATTEMPTING,
    UNDO_BLOCKED_DEST_CHANGED,
    UNDO_BLOCKED_DEST_MISSING,
    UNDO_BLOCKED_DIR_CHANGED,
    UNDO_BLOCKED_SOURCE_OCCUPIED,
    UNDO_PENDING,
    UNDO_RESTORED,
    UNDO_SKIPPED_NOT_MOVED,
    UNDO_STATUS_ABORTED_GLOBAL,
    UNDO_STATUS_ABORTED_LOG_FAILURE,
    UNDO_STATUS_COMPLETED,
    UNDO_STATUS_IN_PROGRESS,
    UNDO_STATUS_NEEDS_REVIEW,
    UNDO_STATUS_PARTIAL,
    UNDO_UNKNOWN,
    derive_snapshot,
    fold_verified_journals,
    read_snapshot,
    write_snapshot,
)

Hook = Callable[[str, dict[str, Any]], None]

#: undo_result 的 reason_code（snapshot 侧由 undo_state 直接映射）
RC_OK = "OK"
RC_RESTORED_IDENTITY_MISMATCH = "RESTORED_IDENTITY_MISMATCH"
RC_DEST_EXISTS = "DEST_EXISTS"
RC_DEST_CHANGED = "DEST_CHANGED"
RC_DEST_MISSING = "DEST_MISSING"
RC_DIR_IDENTITY_CHANGED = "DIR_IDENTITY_CHANGED"
RC_UNKNOWN_STATE = "UNKNOWN_STATE"
RC_PERMISSION_DENIED = "PERMISSION_DENIED"
RC_ATOMIC_UNAVAILABLE = "ATOMIC_MOVE_UNAVAILABLE"
RC_LOG_WRITE_FAILED = "LOG_WRITE_FAILED"


class UndoRefused(RuntimeError):
    """撤销前置条件不满足。"""


@dataclass
class UndoResult:
    plan_id: str
    restored: int = 0
    blocked: int = 0
    skipped_not_moved: int = 0
    unknown: int = 0
    stopped: bool = False
    stop_reason: str | None = None
    undo_status: str = UNDO_STATUS_IN_PROGRESS
    entries: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    restored_dirs: int = 0


def _undo_journal(settings: Settings, plan_id: str) -> Journal:
    return Journal(settings.state_dir, plan_id, "undo")


def _five(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "st_dev": int(identity["st_dev"]),
        "st_ino": int(identity["st_ino"]),
        "size_bytes": int(identity["size_bytes"]),
        "mtime_ns": int(identity["mtime_ns"]),
        "mode_type": str(identity["mode_type"]),
    }


def _entry_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(e["op_id"]): e for e in snapshot.get("entries", [])}


def load_or_rebuild_snapshot(
    settings: Settings,
    plan_id: str,
    *,
    plan: dict[str, Any] | None = None,
    write: bool = True,
) -> tuple[dict[str, Any], ReadReport]:
    """读取快照；缺失时从「执行日志 + 撤销日志」重建（**绝不写 undo_intent**）。

    P1 修复：**先校验两份日志，再重建**。任一日志损坏即抛
    :class:`JournalCorrupted`，绝不把损坏当成「空撤销历史」，
    也不重建/覆盖已有快照。
    """
    snapshot = read_snapshot(settings, plan_id)
    report = ReadReport()
    if snapshot is not None and snapshot.get("entries"):
        return snapshot, report
    plan = plan or load_verified_plan(settings, plan_id)
    exec_fold, undo_fold = fold_verified_journals(settings.state_dir, plan_id)
    snapshot = derive_snapshot(plan=plan, execution_fold=exec_fold, undo_fold=undo_fold)
    if write:
        write_snapshot(settings, plan_id, snapshot)
    return snapshot, report


# --- 5.4.1 中断判定表（attempting 专用，10 行互斥） ---------------------------
def decide_attempting(
    *,
    source_identity: FileIdentity | None,
    dest_identity: FileIdentity | None,
    expected: dict[str, Any],
) -> tuple[str, str, str, str]:
    """返回 ``(undo_state, reason_code, reason, scope)``。

    仅用于 ``attempting`` 条目（已持久化 ``undo_intent``、尚无 ``undo_result``）。
    ``lstat`` 的非 ENOENT 错误必须由调用方先行抛出并映射为 global。
    """
    s_exists = source_identity is not None
    d_exists = dest_identity is not None
    s_match = bool(source_identity is not None and source_identity.matches(expected))
    d_match = bool(dest_identity is not None and dest_identity.matches(expected))

    # 优先级 1：原位置就是本文件且目标已消失 → 确定已还原
    if s_match and not d_exists:
        return (
            UNDO_RESTORED, RC_OK, "已还原：原位置身份与该次撤销的预期身份完全一致", "none"
        )
    # 优先级 2：目标已不在（被移走/删除）→ local 跳过（先于「原位置被占用」判定）
    if not d_exists:
        if s_exists:
            return (
                UNDO_BLOCKED_DEST_MISSING, RC_DEST_MISSING,
                "目标已不在（被移走或删除），原位置另有文件，拒绝覆盖（不报 restored）", "local",
            )
        return (
            UNDO_UNKNOWN, RC_UNKNOWN_STATE,
            "原位置与目标位置都不存在，状态丢失（需人工检查）", "global",
        )
    # 优先级 3：目标存在但身份不符 → local 跳过
    if not d_match:
        return (
            UNDO_BLOCKED_DEST_CHANGED, RC_DEST_CHANGED,
            "目标当前身份与持久化基准不一致（已被改动或替换），拒绝撤销", "local",
        )
    # 目标匹配：再看原位置
    if not s_exists:
        return (
            UNDO_ATTEMPTING, RC_OK, "尚未还原：保留原 undo_intent，续作时复用同一次尝试", "none"
        )
    if s_match:
        return (
            UNDO_UNKNOWN, RC_UNKNOWN_STATE,
            "同一 inode 在原位置与目标位置同时存在，状态不可判（需人工检查）", "global",
        )
    return (
        UNDO_BLOCKED_SOURCE_OCCUPIED, RC_DEST_EXISTS,
        "原位置被不同的文件占用，拒绝覆盖（不报 restored）", "local",
    )


def _probe_dirs(settings: Settings, plan: dict[str, Any]) -> tuple[tuple[int, int], tuple[int, int]]:
    downloads_dir = str(plan["downloads_dir"])
    target_root = str(plan["target_root"])
    downloads_identity = dir_identity(downloads_dir)
    target_identity = dir_identity(target_root) if os.path.isdir(target_root) else (-1, -1)
    return downloads_identity, target_identity


def undo_plan(
    settings: Settings,
    plan_id: str,
    *,
    confirm: bool,
    hook: Hook | None = None,
    lock: GlobalLock | None = None,
    kind: str = "cli",
    resume: bool = True,
) -> UndoResult:
    """撤销一份已执行的计划。``confirm`` 必须为真（用户已再次确认撤销）。

    顺序（P1 修复）：取锁 → 绑定当前配置 → 双日志只读准入 → 能力探测 → 撤销。
    **取锁失败时不得调用探测**（探测会写入/清理状态目录材料）。
    """
    if not confirm:
        raise UndoRefused("撤销需要用户显式确认（--confirm）；拒绝执行")

    own_lock = lock is None
    lock = lock or GlobalLock(settings)
    handle = None
    if own_lock:
        try:
            handle = lock.acquire(kind=kind, action="undo", plan_id=plan_id)
        except LockBusy as exc:
            raise UndoRefused(str(exc)) from exc
    try:
        # 绑定当前配置（含路径形状、链接段与目录身份），拒绝已换配置的旧计划
        try:
            plan = load_verified_plan(settings, plan_id, verify_binding=True)
        except (PlanError, JournalCorrupted) as exc:
            raise UndoRefused(f"计划绑定校验失败，拒绝撤销：{exc}") from exc
        # 双日志只读准入：损坏或**尚需修复**都拒绝，且不做任何移动
        try:
            assert_journals_admissible(settings, plan_id, action="撤销")
        except ExecutionRefused as exc:
            message = str(exc)
            # 结构化区分：需要修复 → needs_review；损坏 → corrupted（不靠消息关键词）
            repairable = any(
                item["state"].repairable
                for item in classify_journals(settings, plan_id).values()
            )
            corrupted = any(
                item["state"].corrupted
                for item in classify_journals(settings, plan_id).values()
            )
            phase = PHASE_CORRUPTED if corrupted else PHASE_NEEDS_REVIEW
            write_plan_state(settings, plan_id, phase=phase, last_error=message)
            raise UndoRefused(message) from exc
        restored_dirs = 0
        if plan.get("schema_version") == 3:
            from .directory_archive import restore_directories, read_folds
            execution, _ = read_folds(settings, plan)
            if any(slot.get("intent") and not slot.get("result") for slot in execution.values()):
                raise UndoRefused("存在执行悬空 intent，请先 recover")
        # 两日志及悬空操作准入后才探测。
        probe_capability(settings)
        if plan.get("schema_version") == 3:
            try:
                if plan["archive"]["enabled"]:
                    AtomicMover().probe_directory_capability(settings.state_dir)
                restored_dirs = restore_directories(settings, plan, hook,resume=resume)
            except Exception as exc:
                write_plan_state(settings, plan_id, phase=PHASE_NEEDS_REVIEW, last_error=str(exc))
                raise UndoRefused(f"目录还原受阻，未撤销文件或更早批次：{exc}") from exc
        result = _undo_locked(settings, plan, hook=hook, resume=resume)
        result.restored_dirs = restored_dirs
        return result
    finally:
        if own_lock:
            lock.release(handle)


def _undo_locked(
    settings: Settings,
    plan: dict[str, Any],
    *,
    hook: Hook | None,
    resume: bool,
) -> UndoResult:
    plan_id = str(plan["plan_id"])
    journal = _undo_journal(settings, plan_id)
    report = ReadReport()
    result = UndoResult(plan_id=plan_id)

    # P1 修复：先校验两份日志（含封存判定），损坏则置 corrupted 并拒绝继续，
    # **不重建、不覆盖**任何已有快照或证据。
    try:
        journal.repair_if_needed(report=report)
        exec_fold, undo_fold = fold_verified_journals(settings.state_dir, plan_id)
    except JournalNeedsRepair as exc:
        raise UndoRefused(
            f"撤销日志尚需一次安全修复（{exc.state.detail if exc.state else exc}）；"
            "请先运行 recover，再执行撤销。"
        ) from exc
    except JournalCorrupted as exc:
        write_plan_state(settings, plan_id, phase=PHASE_CORRUPTED, last_error=str(exc))
        raise UndoRefused(
            f"日志损坏（corrupted），拒绝继续撤销；已保留旧证据与快照：{exc}"
        ) from exc
    result.notes.extend(report.notes)

    # 用已校验的两份日志重新推导快照（合并既有撤销状态）
    snapshot = derive_snapshot(plan=plan, execution_fold=exec_fold, undo_fold=undo_fold)
    write_snapshot(settings, plan_id, snapshot)

    entries = list(snapshot.get("entries", []))

    # P1 修复：批次开始前先识别**已持久化**的全局阻塞（unknown_needs_review /
    # blocked_dir_changed）。存在时不得自动略过并去移动其他文件。
    prior_global = _prior_global_states(entries)
    if prior_global:
        write_plan_state(
            settings, plan_id, phase=PHASE_NEEDS_REVIEW,
            undo_status=UNDO_STATUS_NEEDS_REVIEW,
        )
        result.entries = entries
        result.stopped = True
        result.stop_reason = (
            "存在已持久化的全局不可判/目录异常状态，未解决前不再撤销其他文件："
            + ", ".join(f"{op_id}={state}" for op_id, state in prior_global)
        )
        result.undo_status = UNDO_STATUS_NEEDS_REVIEW
        result.unknown = len(prior_global)
        result.skipped_not_moved = sum(
            1 for e in entries if e.get("undo_state") == UNDO_SKIPPED_NOT_MOVED
        )
        result.notes.append(result.stop_reason)
        return result

    write_plan_state(settings, plan_id, phase=PHASE_UNDOING)
    downloads_identity, target_identity = _probe_dirs(settings, plan)

    run_order = [e for e in reversed(entries) if e.get("undo_state") in (UNDO_PENDING, UNDO_ATTEMPTING)]

    stopped = False
    stop_reason: str | None = None
    log_failure = False

    for entry in run_order:
        if stopped:
            break
        state = str(entry.get("undo_state"))
        if state == UNDO_ATTEMPTING:
            outcome = _resume_attempting(
                settings, journal, plan, entry, result, hook=hook, resume=resume
            )
            if outcome[0] == "global":
                stopped, stop_reason = True, outcome[1]
            continue

        # --- 普通 pending 条目：正常撤销流程 --------------------------------
        op_id = str(entry["op_id"])
        expected = _five(entry["dest_identity"])
        source = str(entry["original_source"])
        dest = str(entry["current_dest"])

        try:
            if plan.get("schema_version") == 3:
                from .directory_archive import verify_binding
                verify_binding(settings, plan)
            if dir_identity(str(plan["downloads_dir"])) != downloads_identity:
                raise DirectoryIdentityChanged("下载目录身份发生变化")
            if os.path.isdir(str(plan["target_root"])) and dir_identity(str(plan["target_root"])) != target_identity:
                raise DirectoryIdentityChanged("目标根身份发生变化")
            assert_no_symlink_segment_for_file(dest, str(plan["target_root"]))
            assert_no_symlink_segment_for_file(source, str(plan["downloads_dir"]))
            source_identity = lstat_identity(source)
            dest_identity = lstat_identity(dest)
        except DirectoryIdentityChanged as exc:
            # 预检阶段即被阻止：写 undo_skip（此时尚无 undo_intent，不能写孤立 undo_result）
            journal.append(
                make_event(
                    "undo_skip", op_id, undo_state=UNDO_BLOCKED_DIR_CHANGED,
                    reason_code=RC_DIR_IDENTITY_CHANGED, reason=str(exc),
                    expected_identity=expected,
                ),
                report=report,
            )
            _set_entry_state(entry, UNDO_BLOCKED_DIR_CHANGED, str(exc))
            stopped, stop_reason = True, f"{RC_DIR_IDENTITY_CHANGED}: {exc}"
            continue
        except OSError as exc:
            # 任何非 ENOENT 的判定错误都必须阻止操作（不得当作「不存在」）
            journal.append(
                make_event(
                    "undo_skip", op_id, undo_state=UNDO_UNKNOWN,
                    reason_code=RC_UNKNOWN_STATE, reason=f"无法判定状态：{exc}",
                    expected_identity=expected,
                ),
                report=report,
            )
            _set_entry_state(entry, UNDO_UNKNOWN, f"无法判定状态：{exc}")
            stopped, stop_reason = True, f"无法判定状态：{exc}"
            continue

        # 预检判定与 5.4.1 判定表使用同一优先级：
        # 目标不存在 / 目标身份不符 → local 跳过；目标匹配后再看原位置。
        source_matches_expected = bool(
            source_identity is not None and source_identity.matches(expected)
        )
        state_now: str | None
        if source_matches_expected and dest_identity is None:
            state_now, code, reason = None, RC_OK, ""  # 用户已自行还原 → 交给 attempt 分支补记 restored
        elif dest_identity is None:
            state_now, code, reason = (
                UNDO_BLOCKED_DEST_MISSING, RC_DEST_MISSING,
                "目标已不在（被移走或删除）" if source_identity is None
                else "目标已不在（被移走或删除），原位置另有文件，拒绝覆盖",
            )
        elif not dest_identity.matches(expected):
            state_now, code, reason = (
                UNDO_BLOCKED_DEST_CHANGED, RC_DEST_CHANGED,
                "目标当前身份与持久化基准不一致（已被改动或替换）",
            )
        elif source_identity is not None and not source_matches_expected:
            state_now, code, reason = (
                UNDO_BLOCKED_SOURCE_OCCUPIED, RC_DEST_EXISTS, "原位置已被占用，未覆盖",
            )
        elif source_identity is not None:
            state_now, code, reason = (
                UNDO_UNKNOWN, RC_UNKNOWN_STATE,
                "同一 inode 在原位置与目标位置同时存在，状态不可判（需人工检查）",
            )
        else:
            state_now, code, reason = None, RC_OK, ""

        if state_now is not None:
            # 预检即被阻止：写 undo_skip（不产生孤立 result）
            try:
                journal.append(
                    make_event(
                        "undo_skip", op_id, undo_state=state_now, reason_code=code,
                        reason=reason, expected_identity=expected,
                    ),
                    report=report,
                )
            except (JournalWriteFailed, JournalCorrupted) as exc:
                result.notes.append(f"{op_id}: undo_skip 落盘失败：{exc}")
                log_failure = True
                stopped, stop_reason = True, f"撤销日志写入失败：{exc}"
                continue
            _set_entry_state(entry, state_now, reason)
            if state_now in LOCAL_UNDO_STATES:
                result.blocked += 1
            else:
                result.unknown += 1
                stopped, stop_reason = True, f"{state_now}: {reason}"
            continue

        # --- 通过预检：写唯一 undo_intent，进入 attempting ------------------
        try:
            journal.append(
                make_event(
                    "undo_intent", op_id,
                    **{"from": dest, "to": source, "expected_identity": expected},
                ),
                report=report,
            )
        except (JournalWriteFailed, JournalCorrupted) as exc:
            log_failure = True
            stopped, stop_reason = True, f"undo_intent 落盘失败，拒绝移动：{exc}"
            continue

        _set_entry_state(entry, UNDO_ATTEMPTING, "已写入 undo_intent，正在尝试还原")
        ok, code, reason, scope = _perform_restore(
            settings, journal, plan, entry, expected, source, dest, hook=hook
        )
        if ok:
            result.restored += 1
            _set_entry_state(entry, UNDO_RESTORED, reason)
        elif scope == "local":
            result.blocked += 1
            _set_entry_state(entry, code, reason)
        else:
            result.unknown += 1
            _set_entry_state(entry, code, reason)
            stopped, stop_reason = True, f"{code}: {reason}"

    # 重算整批状态并写回快照
    snapshot["entries"] = entries
    status = _aggregate(entries + snapshot.get("directory_entries", []), log_failure)
    snapshot["undo_status"] = status
    write_snapshot(settings, plan_id, snapshot)

    result.entries = entries
    result.stopped = stopped
    result.stop_reason = stop_reason
    result.undo_status = status
    result.skipped_not_moved = sum(1 for e in entries if e.get("undo_state") == UNDO_SKIPPED_NOT_MOVED)
    result.unknown += sum(
        1 for e in entries if e.get("undo_state") == UNDO_UNKNOWN and e not in run_order
    )

    phase = {
        UNDO_STATUS_COMPLETED: PHASE_UNDONE,
        UNDO_STATUS_PARTIAL: PHASE_PARTIAL_UNDO,
        UNDO_STATUS_ABORTED_GLOBAL: PHASE_ABORTED_GLOBAL,
        UNDO_STATUS_ABORTED_LOG_FAILURE: PHASE_ABORTED_LOG_FAILURE,
        UNDO_STATUS_NEEDS_REVIEW: PHASE_NEEDS_REVIEW,
    }.get(status, PHASE_PARTIAL_UNDO)
    write_plan_state(settings, plan_id, phase=phase, undo_status=status)
    return result


def _write_undo_result(
    journal: Journal, op_id: str, *, outcome: str, reason_code: str,
    undo_state: str, reason: str, **extra: Any
) -> None:
    """写一条 ``undo_result``（带 ``undo_state`` 便于快照直接物化）。"""
    payload: dict[str, Any] = {
        "outcome": outcome,
        "reason_code": reason_code,
        "undo_state": undo_state,
        "reason": reason,
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    journal.append(make_event("undo_result", op_id, **payload), report=None)


def _prior_global_states(entries: list[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """返回条目中**已持久化**的全局阻塞状态（op_id, state）。

    ``unknown_needs_review`` 与 ``blocked_dir_changed`` 属于全局问题：
    在它们被明确解决之前，不得继续撤销其他文件（P1 修复）。
    """
    return [
        (str(entry.get("op_id")), str(entry.get("undo_state")))
        for entry in entries
        if entry.get("undo_state") in GLOBAL_UNDO_STATES
    ]


def _set_entry_state(entry: dict[str, Any], state: str, reason: str) -> None:
    entry["undo_state"] = state
    if reason:
        entry["undo_reason"] = reason
    if state == UNDO_ATTEMPTING:
        entry["undo_attempts"] = int(entry.get("undo_attempts") or 0)


def _perform_restore(
    settings: Settings,
    journal: Journal,
    plan: dict[str, Any],
    entry: dict[str, Any],
    expected: dict[str, Any],
    source: str,
    dest: str,
    *,
    hook: Hook | None,
) -> tuple[bool, str, str, str]:
    """intent 之后重检安全条件并调用原语；返回 ``(ok, state_or_code, reason, scope)``。"""
    op_id = str(entry["op_id"])
    dest_fd = src_fd = None
    try:
        # intent 之后重检（防 TOCTOU；原语仍是不覆盖的最终保证）
        if lstat_identity(source) is not None:
            raise DestinationExists("原位置已被占用，未覆盖")
        current = lstat_identity(dest)
        if current is None:
            raise FileNotFoundError(dest)
        if not current.matches(expected):
            journal.append(
                make_event(
                    "undo_result", op_id, outcome="failed", reason_code=RC_DEST_CHANGED,
                    undo_state=UNDO_BLOCKED_DEST_CHANGED, reason="目标已被改动，拒绝撤销",
                ),
                report=None,
            )
            return False, UNDO_BLOCKED_DEST_CHANGED, "目标已被改动，拒绝撤销", "local"

        dest_dir = os.path.dirname(dest)
        source_dir = os.path.dirname(source)
        dest_fd = open_dir_fd(dest_dir)
        src_fd = open_dir_fd(source_dir)
        if fd_identity(src_fd)[0] != fd_identity(dest_fd)[0]:
            raise MoveFailed(errno.EXDEV, "源与目标不在同一文件系统")

        if hook is not None:
            hook("before_undo_move", {"op_id": op_id, "from": dest, "to": source})

        if plan.get("schema_version") == 3:
            from .directory_archive import verify_binding
            try:
                verify_binding(settings,plan)
            except PlanError as exc:
                raise DirectoryIdentityChanged(str(exc)) from exc
        AtomicMover().rename_excl_at(dest_fd, os.path.basename(dest), src_fd, os.path.basename(source))
    except DestinationExists as exc:
        journal.append(
            make_event(
                "undo_result", op_id, outcome="skipped", reason_code=RC_DEST_EXISTS,
                undo_state=UNDO_BLOCKED_SOURCE_OCCUPIED, reason=str(exc),
            ),
            report=None,
        )
        return False, UNDO_BLOCKED_SOURCE_OCCUPIED, str(exc), "local"
    except FileNotFoundError:
        journal.append(
            make_event(
                "undo_result", op_id, outcome="skipped", reason_code=RC_DEST_MISSING,
                undo_state=UNDO_BLOCKED_DEST_MISSING, reason="目标已不在（被移走或删除）",
            ),
            report=None,
        )
        return False, UNDO_BLOCKED_DEST_MISSING, "目标已不在（被移走或删除）", "local"
    except DirectoryIdentityChanged as exc:
        journal.append(make_event("undo_result",op_id,outcome="failed",reason_code=RC_DIR_IDENTITY_CHANGED,undo_state=UNDO_BLOCKED_DIR_CHANGED,reason=str(exc)),report=None)
        return False, UNDO_BLOCKED_DIR_CHANGED,str(exc),"global"
    except AtomicMoveUnavailable as exc:
        journal.append(
            make_event(
                "undo_result", op_id, outcome="failed", reason_code=RC_ATOMIC_UNAVAILABLE,
                undo_state=UNDO_UNKNOWN, reason=str(exc),
            ),
            report=None,
        )
        return False, UNDO_UNKNOWN, f"原语不可用：{exc}", "global"
    except MoveFailed as exc:
        if exc.errno == errno.EEXIST:
            state, code, scope = UNDO_BLOCKED_SOURCE_OCCUPIED, RC_DEST_EXISTS, "local"
        elif exc.errno in (errno.EACCES, errno.EPERM):
            state, code, scope = UNDO_UNKNOWN, RC_PERMISSION_DENIED, "global"
        elif exc.errno == errno.EXDEV:
            state, code, scope = UNDO_UNKNOWN, "CROSS_FILESYSTEM", "global"
        else:
            state, code, scope = UNDO_UNKNOWN, RC_UNKNOWN_STATE, "global"
        journal.append(
            make_event(
                "undo_result", op_id, outcome="failed", reason_code=code,
                undo_state=state, reason=str(exc),
            ),
            report=None,
        )
        return False, state, str(exc), scope
    except OSError as exc:
        journal.append(
            make_event(
                "undo_result", op_id, outcome="failed", reason_code=RC_UNKNOWN_STATE,
                undo_state=UNDO_UNKNOWN, reason=f"无法判定或移动失败：{exc}",
            ),
            report=None,
        )
        return False, UNDO_UNKNOWN, f"无法判定或移动失败：{exc}", "global"
    finally:
        for fd in (dest_fd, src_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    # P1 修复：原语返回成功后，**必须核对原位置**是否为普通文件且五项身份等于
    # 该次撤销的预期基准。只做移动前预检不足以发现「检查后目标被替换」——
    # 此时被搬回的可能是替换物，绝不能记录 restored。
    try:
        restored_identity = lstat_identity(source)
    except OSError as exc:
        _write_undo_result(
            journal, op_id, outcome="failed", reason_code=RC_UNKNOWN_STATE,
            undo_state=UNDO_UNKNOWN,
            reason=f"移动后无法核对原位置身份，全局停止（不重设基准）：{exc}",
        )
        return False, UNDO_UNKNOWN, f"移动后无法核对原位置身份：{exc}", "global"

    if restored_identity is None or restored_identity.mode_type != "regular":
        _write_undo_result(
            journal, op_id, outcome="failed", reason_code=RC_RESTORED_IDENTITY_MISMATCH,
            undo_state=UNDO_UNKNOWN,
            reason="移动后原位置不是普通文件，全局停止（不重设基准、不自动回滚）",
            expected_identity=expected,
            observed_identity=(restored_identity.as_dict() if restored_identity else None),
        )
        return (
            False, UNDO_UNKNOWN,
            f"{op_id}: 原位置不是普通文件，不能记为 restored", "global",
        )

    if not restored_identity.matches(expected):
        _write_undo_result(
            journal, op_id, outcome="failed", reason_code=RC_RESTORED_IDENTITY_MISMATCH,
            undo_state=UNDO_UNKNOWN,
            reason=(
                "移动后原位置五项身份与该次撤销的预期基准不一致（目标在检查后被替换），"
                "全局停止：不重设基准、不盲目自动回滚"
            ),
            expected_identity=expected,
            observed_identity=restored_identity.as_dict(),
        )
        return (
            False, UNDO_UNKNOWN,
            f"{op_id}: 还原后身份不匹配（原位置不是预期文件），全局停止", "global",
        )

    _write_undo_result(
        journal, op_id, outcome="restored", reason_code=RC_OK,
        undo_state=UNDO_RESTORED, reason="已还原到原位置（五项身份已核对）",
        expected_identity=expected, observed_identity=restored_identity.as_dict(),
    )
    return True, UNDO_RESTORED, "已还原到原位置（五项身份已核对）", "none"


def _resume_attempting(
    settings: Settings,
    journal: Journal,
    plan: dict[str, Any],
    entry: dict[str, Any],
    result: UndoResult,
    *,
    hook: Hook | None,
    resume: bool,
) -> tuple[str, str]:
    """中断恢复：只针对 ``attempting``（已完成 5.4.1 判定）。

    返回 ``("ok", "")`` 或 ``("global", reason)``。
    """
    op_id = str(entry["op_id"])
    expected = _five(entry["dest_identity"])
    source = str(entry["original_source"])
    dest = str(entry["current_dest"])

    try:
        source_identity = lstat_identity(source)
        dest_identity = lstat_identity(dest)
    except OSError as exc:
        state, code, reason, scope = UNDO_UNKNOWN, RC_UNKNOWN_STATE, f"无法判定状态：{exc}", "global"
        journal.append(
            make_event("undo_result", op_id, outcome="failed", reason_code=code, undo_state=state, reason=reason),
            report=None,
        )
        _set_entry_state(entry, state, reason)
        result.unknown += 1
        return "global", reason

    state, code, reason, scope = decide_attempting(
        source_identity=source_identity, dest_identity=dest_identity, expected=expected
    )

    if state == UNDO_ATTEMPTING:
        # 保留原 intent；用户确认续作时复用同一次尝试，不追加第二条 intent
        if not resume:
            result.notes.append(f"{op_id}: 保持 attempting（未获续作确认）")
            return "ok", ""
        ok, rcode, rreason, rscope = _perform_restore(
            settings, journal, plan, entry, expected, source, dest, hook=hook
        )
        if ok:
            result.restored += 1
            _set_entry_state(entry, UNDO_RESTORED, rreason)
            return "ok", ""
        if rscope == "local":
            result.blocked += 1
            _set_entry_state(entry, rcode, rreason)
            return "ok", ""
        result.unknown += 1
        _set_entry_state(entry, rcode, rreason)
        return "global", rreason

    journal.append(
        make_event("undo_result", op_id, outcome="restored" if state == UNDO_RESTORED else "skipped",
                   reason_code=code, undo_state=state, reason=reason),
        report=None,
    )
    _set_entry_state(entry, state, reason)
    if state == UNDO_RESTORED:
        result.restored += 1
        return "ok", ""
    if scope == "local":
        result.blocked += 1
        return "ok", ""
    result.unknown += 1
    return "global", reason


def _aggregate(entries: list[dict[str, Any]], log_failure: bool) -> str:
    if log_failure:
        return UNDO_STATUS_ABORTED_LOG_FAILURE
    relevant = [e for e in entries if e.get("undo_state") != UNDO_SKIPPED_NOT_MOVED]
    if not relevant:
        return UNDO_STATUS_COMPLETED
    if any(e.get("undo_state") in GLOBAL_UNDO_STATES for e in relevant):
        return UNDO_STATUS_NEEDS_REVIEW
    if all(e.get("undo_state") == UNDO_RESTORED for e in relevant):
        return UNDO_STATUS_COMPLETED
    if any(e.get("undo_state") in (UNDO_PENDING, UNDO_ATTEMPTING) for e in relevant):
        return UNDO_STATUS_IN_PROGRESS
    return UNDO_STATUS_PARTIAL


def undo_summary(result: UndoResult) -> str:
    lines = [
        f"撤销 {result.plan_id}：已还原 {result.restored} · 被阻止 {result.blocked} · "
        f"无需还原 {result.skipped_not_moved} · 不可判 {result.unknown}"
    ]
    lines.append(f"整批状态：{result.undo_status}")
    if result.stopped and result.stop_reason:
        lines.append(f"已停止：{result.stop_reason}（未处理项保持 pending）")
    for entry in result.entries:
        lines.append(f"  {entry['op_id']} {entry['undo_state']} {entry.get('undo_reason') or ''}")
    if result.restored_dirs:
        lines.append(f"目录还原：{result.restored_dirs} 个原目录")
    for note in result.notes:
        lines.append(f"  · {note}")
    return "\n".join(lines)
