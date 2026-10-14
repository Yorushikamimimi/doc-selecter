"""执行器：确认校验 → 全局锁 → 逐项持久化 → 原子移动 → 恢复 → 撤销快照。

严格落实 docs/MVP方案.md §5.3.1 / §5.6.3 / §5.5 / §7.2：

- 前置条件任一不满足 → **零移动**（锁、确认指纹、计划状态、未解析 intent、能力探测）；
- 逐项顺序固定：写 ``intent`` + fsync → 预检 → ``RENAME_EXCL`` → 写 ``result`` + fsync；
- ``local`` 失败继续，``global`` 失败停止整批（未处理项标 ``not_attempted``）；
- 移动后校验目标 inode 与预期源身份一致，不一致 → ``MOVE_IDENTITY_MISMATCH``（global）。
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from .atomic_move import (
    AtomicMover,
    AtomicMoveUnavailable,
    DestinationExists,
    DirectoryIdentityChanged,
    FileIdentity,
    MoveFailed,
    OutOfScopeError,
    SourceChangedError,
    SourceMissing,
    assert_no_symlink_segment_for_file,
    dir_identity,
    fd_identity,
    lstat_identity,
    open_dir_fd,
)
from .config import Settings
from .journal import (
    JOURNAL_CORRUPTED,
    Journal,
    JournalCorrupted,
    JournalNeedsRepair,
    JournalState,
    JournalWriteFailed,
    ReadReport,
    make_event,
)
from .locking import (
    EXECUTE_BLOCKED_PHASES,
    GlobalLock,
    LockBusy,
    PHASE_ABORTED_LOG_FAILURE,
    PHASE_CORRUPTED,
    TERMINAL_ABNORMAL_PHASES,
    PHASE_EXECUTED,
    PHASE_EXECUTING,
    PHASE_NEEDS_REVIEW,
    PHASE_LOCKED,
    PHASE_PREPARED,
    current_phase,
    read_plan_state,
    write_plan_state,
)
from .planner import (MoveAdmissionRefused, _MISSING,
                      directory_identity_key, normalized_dir_identity_map,
                      load_verified_plan, read_confirmation, verify_plan_binding)
from .snapshot import (
    TERMINAL_UNDO_STATES,
    derive_snapshot,
    fold_verified_journals,
    write_snapshot,
)

Hook = Callable[[str, dict[str, Any]], None]


class ExecutionRefused(RuntimeError):
    """前置条件不满足，拒绝执行（零移动）。"""


@dataclass
class ExecutionResult:
    plan_id: str
    moved: int = 0
    skipped: int = 0
    failed: int = 0
    not_attempted: int = 0
    stopped: bool = False
    stop_reason: str | None = None
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    recovery_notes: list[str] = field(default_factory=list)
    phase: str = PHASE_PREPARED

    @property
    def total_processed(self) -> int:
        return self.moved + self.skipped + self.failed + self.not_attempted


def _journal(settings: Settings, plan_id: str) -> Journal:
    return Journal(settings.state_dir, plan_id, "execution")


def validate_confirmation(settings: Settings, plan: dict[str, Any], confirm_hash: str | None) -> None:
    """确认指纹必须与当前计划一致，且与确认记录一致。"""
    plan_id = str(plan["plan_id"])
    current = str(plan.get("plan_hash"))
    if not confirm_hash:
        raise ExecutionRefused("缺少 --confirm 计划指纹；拒绝执行（零移动）")
    if confirm_hash != current:
        raise ExecutionRefused(
            f"确认指纹与当前计划不一致（提交 {confirm_hash}，当前 {current}）；"
            "方案已变化，请重新生成预览并确认。"
        )
    record = read_confirmation(settings, plan_id)
    if record is None:
        raise ExecutionRefused("未找到确认记录；请先确认该计划（拒绝执行）")
    if str(record.get("confirmed_plan_hash")) != current:
        raise ExecutionRefused(
            "确认记录中的指纹与当前计划不一致（计划被改动）；请重新确认。"
        )


def probe_capability(settings: Settings) -> None:
    """能力探测：不通过即拒绝整批移动（零移动）。

    使用 ``AtomicMover.probe_capability`` 的**唯一私有探测目录**实现：
    不删除任何历史 ``.probe-*`` 文件，只清理本次创建的材料。
    """
    mover = AtomicMover()
    if not mover.available:
        raise ExecutionRefused(
            f"当前环境不支持原子不覆盖移动（{mover.unavailable_reason}）；"
            "为避免覆盖风险，本工具拒绝执行移动。"
        )
    try:
        mover.probe_capability(settings.state_dir)
    except AtomicMoveUnavailable as exc:
        raise ExecutionRefused(
            f"当前环境不支持原子不覆盖移动（{exc}）；"
            "为避免覆盖风险，本工具拒绝执行移动。"
        ) from exc


def classify_journals(settings: Settings, plan_id: str) -> dict[str, dict[str, Any]]:
    """**无副作用**地分类两份日志（healthy / repairable / corrupted）。

    除了 :meth:`Journal.classify` 的内容/结构分类，还会在**整条跨代逻辑事件流**上
    做一次只读校验（``read_events(allow_repair=False)``），因此「物理代次各自合法、
    但拼接后出现跨代重复或孤立事件」也会被判为 ``corrupted``。不创建目录、
    不写清单、不改动任何字节。返回 ``{kind: {"state": JournalState, "label": str}}``。
    """
    out: dict[str, dict[str, Any]] = {}
    for kind in ("execution", "undo"):
        journal = Journal(settings.state_dir, plan_id, kind)
        label = "执行日志" if kind == "execution" else "撤销日志"
        state = journal.classify()
        if not state.corrupted:
            # 跨代逻辑流校验：healthy/repairable 都必须能拼成合法的逻辑事件流
            try:
                journal.read_events(allow_repair=False)
            except JournalNeedsRepair as exc:
                state = (
                    exc.state
                    if exc.state is not None and exc.state.repairable
                    else JournalState(
                        JOURNAL_CORRUPTED, exc.state.detail if exc.state else str(exc)
                    )
                )
            except JournalCorrupted as exc:
                state = JournalState(JOURNAL_CORRUPTED, str(exc))
        out[kind] = {"state": state, "label": label}
    return out


def assert_journals_admissible(settings: Settings, plan_id: str, *, action: str) -> None:
    """**无副作用**地校验两份日志，作为**移动/补写事件**的前置准入。

    ``healthy`` 通过；``repairable`` 也通过（表示日志本身合法、只是尚需一次
    方案允许的修复；修复动作由调用方在**两份都验证过之后**显式执行）；
    ``corrupted`` 拒绝。调用方必须已持锁，且本函数先于任何文件操作执行。
    """
    classified = classify_journals(settings, plan_id)
    for kind, item in classified.items():
        state = item["state"]
        if state.corrupted:
            raise ExecutionRefused(
                f"{item['label']}损坏，拒绝{action}（未做任何移动/补写）：{state.detail}"
            )
        if state.repairable:
            raise ExecutionRefused(
                f"{item['label']}尚需一次安全修复（{state.detail}）；"
                f"请先运行 recover，再{action}。"
            )


def assert_journals_repairable(settings: Settings, plan_id: str, *, action: str) -> dict[str, Any]:
    """recover 专用准入：两份日志都必须 healthy 或 repairable。

    任一份 ``corrupted`` → 抛 :class:`ExecutionRefused`，**零修复、零封存、
    零补写、零快照替换**（调用方据此记录 ``corrupted``）。只有本函数通过后，
    调用方才允许执行明确修复。
    """
    classified = classify_journals(settings, plan_id)
    for kind, item in classified.items():
        state = item["state"]
        if state.corrupted:
            raise ExecutionRefused(
                f"{item['label']}损坏，拒绝{action}（未做任何修复/补写）：{state.detail}"
            )
    return classified


def _assert_admissible(settings: Settings, plan_id: str) -> None:
    phase = current_phase(settings, plan_id)
    if phase in EXECUTE_BLOCKED_PHASES:
        raise ExecutionRefused(f"计划状态为 {phase}，禁止再次执行；如需重来请重新生成预览。")
    journal = _journal(settings, plan_id)
    try:
        fold, report = journal.fold(allow_repair=False)
    except JournalNeedsRepair as exc:
        raise ExecutionRefused(
            f"执行日志尚需一次安全修复（{exc.state.detail if exc.state else exc}）；"
            "请先运行 recover，再执行本计划。"
        ) from exc
    except JournalCorrupted as exc:
        raise ExecutionRefused(f"执行日志损坏，拒绝执行：{exc}") from exc
    unresolved = [op for op, slot in fold.items() if slot["intent"] is not None and slot["result"] is None]
    if unresolved or report.truncated_op_id:
        raise ExecutionRefused(
            "执行日志中存在未解析的 intent（"
            + ", ".join(sorted(unresolved)[:5])
            + "）；必须先运行 recover 完成恢复，不得开始新的执行。"
        )
    already = sorted(op for op, slot in fold.items() if slot["result"] is not None)
    if already:
        raise ExecutionRefused(
            "该计划的执行日志中已有结果记录（"
            + ", ".join(already[:5])
            + "）：同一计划只允许执行一次，如需重来请重新生成预览。"
        )


def _precheck_source(operation: dict[str, Any], downloads_dir: str) -> tuple[str, int, str]:
    """返回 ``(source_parent, source_parent_fd 的 st_dev, source_name)``。"""
    source = str(operation["source"])
    assert_no_symlink_segment_for_file(source, downloads_dir)
    parent = os.path.dirname(source)
    name = os.path.basename(source)
    actual = lstat_identity(source)
    if actual is None:
        raise SourceMissing(f"源文件已不存在：{source}")
    if actual.mode_type == "symlink":
        raise OutOfScopeError(f"源是符号链接，拒绝移动：{source}")
    if actual.mode_type != "regular":
        raise SourceChangedError(f"源不是普通文件：{source}")
    if not actual.matches(dict(operation["identity"])):
        raise SourceChangedError(f"源文件身份与计划不符：{source}")
    fd = open_dir_fd(parent)
    return parent, fd, name


def _precheck_dest(operation: dict[str, Any], target_root: str, source_dev: int) -> tuple[str, int, str]:
    dest = str(operation["dest"])
    assert_no_symlink_segment_for_file(dest, target_root)
    parent = os.path.dirname(dest)
    name = os.path.basename(dest)
    if os.path.islink(parent):
        raise OutOfScopeError(f"目标目录是符号链接，拒绝：{parent}")
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = open_dir_fd(parent)
    if fd_identity(fd)[0] != source_dev:
        os.close(fd)
        raise MoveFailed(errno.EXDEV, "源与目标不在同一文件系统（拒绝移动）")
    # 目标状态判定：只有 ENOENT 才表示不存在
    try:
        os.lstat(dest)
    except FileNotFoundError:
        pass
    except OSError as exc:
        os.close(fd)
        raise MoveFailed(exc.errno or errno.EIO, f"无法判定目标状态：{dest}（{exc}）") from exc
    else:
        os.close(fd)
        raise DestinationExists(f"目标已存在：{dest}")
    return parent, fd, name


def _classify_failure(exc: Exception) -> tuple[str, str, str]:
    """把异常映射为 ``(outcome, reason_code, scope)``；scope ∈ {local, global}。"""
    if isinstance(exc, DestinationExists):
        return "skipped", "DEST_EXISTS", "local"
    if isinstance(exc, SourceMissing):
        return "skipped", "SOURCE_MISSING", "local"
    if isinstance(exc, SourceChangedError):
        return "skipped", "SOURCE_CHANGED", "local"
    if isinstance(exc, OutOfScopeError):
        return "skipped", "OUT_OF_SCOPE_PATH", "local"
    if isinstance(exc, DirectoryIdentityChanged):
        return "failed", "DIR_IDENTITY_CHANGED", "global"
    if isinstance(exc, AtomicMoveUnavailable):
        return "failed", "ATOMIC_MOVE_UNAVAILABLE", "global"
    if isinstance(exc, MoveFailed):
        return _classify_errno(exc.errno)
    if isinstance(exc, OSError) and exc.errno:
        # 权限/I/O 错误必须阻止操作，且不得被当作「目标不存在」
        return _classify_errno(exc.errno)
    return "failed", "UNKNOWN_ERROR", "global"


def _classify_errno(err) -> tuple[str, str, str]:
    if err == errno.EXDEV:
        return "skipped", "CROSS_FILESYSTEM", "local"
    if err in (errno.EACCES, errno.EPERM):
        return "failed", "PERMISSION_DENIED", "local"
    if err in (errno.ENOSPC, errno.EDQUOT):
        return "failed", "SPACE_EXHAUSTED", "global"
    if err == errno.EROFS:
        return "failed", "READ_ONLY_FILESYSTEM", "global"
    if err == errno.ENOENT:
        return "skipped", "SOURCE_MISSING", "local"
    if err == errno.EEXIST:
        return "skipped", "DEST_EXISTS", "local"
    return "failed", "ATOMIC_MOVE_FAILED", "global"


def execute_plan(
    settings: Settings,
    plan_id: str,
    confirm_hash: str | None,
    *,
    hook: Hook | None = None,
    lock: GlobalLock | None = None,
    kind: str = "cli",
) -> ExecutionResult:
    """执行一份已确认的计划。

    调用方若已持锁（如 GUI），可传入 ``lock``；否则本函数自行取锁，取不到即拒绝。
    """
    own_lock = lock is None
    lock = lock or GlobalLock(settings)
    handle = None
    if own_lock:
        try:
            handle = lock.acquire(kind=kind, action="execute", plan_id=plan_id)
        except LockBusy as exc:
            raise ExecutionRefused(str(exc)) from exc
    try:
        # 顺序固定：新移动名称准入 → 绑定当前配置 → 双日志准入 → 阶段准入 → 能力探测 → 移动。
        # 日志准入放在阶段准入之前：需要修复时给出「先运行 recover」而不是「已执行」。
        try:
            plan = load_verified_plan(settings, plan_id, for_new_move=True)
        except MoveAdmissionRefused as exc:
            raise ExecutionRefused(str(exc)) from exc
        verify_plan_binding(settings, plan)
        validate_confirmation(settings, plan, confirm_hash)
        assert_journals_admissible(settings, plan_id, action="执行")
        _assert_admissible(settings, plan_id)
        probe_capability(settings)
        return _execute_locked(settings, plan, hook=hook, lock=lock)
    finally:
        if own_lock:
            lock.release(handle)


def _execute_locked(
    settings: Settings, plan: dict[str, Any], *, hook: Hook | None, lock: GlobalLock
) -> ExecutionResult:
    plan_id = str(plan["plan_id"])
    journal = _journal(settings, plan_id)
    result = ExecutionResult(plan_id=plan_id)
    report = ReadReport()

    downloads_dir = str(plan["downloads_dir"])
    target_root = str(plan["target_root"])
    write_plan_state(
        settings, plan_id,
        plan_hash=plan.get("plan_hash"),
        phase=PHASE_LOCKED,
        owner={"pid": os.getpid(), "kind": "cli"},
    )

    downloads_identity = dir_identity(downloads_dir)
    os.makedirs(target_root, mode=0o700, exist_ok=True)
    target_identity = dir_identity(target_root)
    # 计划生成时记录的「目标父目录链」身份（相对 目标根 的标签 → identity）
    planned_dest_identities: dict[str, Any] = normalized_dir_identity_map(
        (plan.get("dir_identities") or {}).get("dest_dirs") or {}, "dest_dirs"
    )
    # 尚不存在的目标目录在本批次首次创建时绑定；既有目录保留计划身份。
    runtime_dest_identities = dict(planned_dest_identities)

    write_plan_state(settings, plan_id, phase=PHASE_EXECUTING)

    operations = list(plan.get("operations", []))
    stopped = False
    stop_reason: str | None = None
    log_failure = False

    for operation in operations:
        op_id = str(operation["op_id"])
        if stopped:
            result.not_attempted += 1
            continue

        event = make_event(
            "intent",
            op_id,
            plan_hash=plan.get("plan_hash"),
            source=operation["source"],
            dest=operation["dest"],
            category=operation.get("category"),
            identity=operation["identity"],
        )
        try:
            journal.append(event, report=report)
        except (JournalWriteFailed, JournalCorrupted) as exc:
            # intent 未落盘 → 该文件绝不移动
            result.failed += 1
            result.outcomes.append(
                {"op_id": op_id, "outcome": "failed", "reason_code": "LOG_WRITE_FAILED", "reason": str(exc)}
            )
            result.stopped = True
            log_failure = True
            result.stop_reason = f"日志写入失败：{exc}"
            write_plan_state(settings, plan_id, phase=PHASE_ABORTED_LOG_FAILURE, last_error=str(exc))
            stopped = True
            stop_reason = result.stop_reason
            continue

        outcome: str
        reason_code: str
        reason: str
        scope = "local"
        dest_identity: dict[str, Any] | None = None
        src_fd = dst_fd = None
        try:
            _, src_fd, source_name = _precheck_source(operation, downloads_dir)
            src_dev = fd_identity(src_fd)[0]
            # 在创建父目录之前核对每一层已有身份。
            dest_parent = os.path.dirname(str(operation["dest"]))
            label = directory_identity_key(os.path.relpath(dest_parent, target_root).replace(os.sep, "/"))
            chain = ["/".join(label.split("/")[:i]) for i in range(1, len(label.split("/")) + 1)]
            for rel in chain:
                expected = runtime_dest_identities.get(rel, _MISSING)
                if expected is _MISSING:
                    raise DirectoryIdentityChanged(f"计划缺少目标父目录身份记录：{rel}")
                path = os.path.join(target_root, *rel.split("/"))
                if expected is not None and dir_identity(path) != tuple(expected):
                    raise DirectoryIdentityChanged(f"目标父目录身份发生变化：{rel}")
            _, dst_fd, dest_name = _precheck_dest(operation, target_root, src_dev)
            for rel in chain:
                path = os.path.join(target_root, *rel.split("/"))
                actual = dir_identity(path)
                expected = runtime_dest_identities[rel]
                if expected is not None and actual != tuple(expected):
                    raise DirectoryIdentityChanged(f"目标父目录身份发生变化：{rel}")
                runtime_dest_identities[rel] = list(actual)

            # 目录身份校验（下载目录 / 目标根 / 类别目录）
            if dir_identity(downloads_dir) != downloads_identity:
                raise DirectoryIdentityChanged("下载目录身份发生变化")
            if dir_identity(target_root) != target_identity:
                raise DirectoryIdentityChanged("目标根身份发生变化")
            if hook is not None:
                hook("before_move", {"op_id": op_id, "source": operation["source"], "dest": operation["dest"]})

            mover = AtomicMover()
            mover.rename_excl_at(src_fd, source_name, dst_fd, dest_name)
        except Exception as exc:  # noqa: BLE001 - 分类映射见 _classify_failure
            outcome, reason_code, scope = _classify_failure(exc)
            reason = str(exc)
        else:
            actual = lstat_identity(str(operation["dest"]))
            expected = FileIdentity(
                st_dev=int(operation["identity"]["st_dev"]),
                st_ino=int(operation["identity"]["st_ino"]),
                size_bytes=int(operation["identity"]["size_bytes"]),
                mtime_ns=int(operation["identity"]["mtime_ns"]),
                mode_type=str(operation["identity"]["mode_type"]),
            )
            if actual is None or not actual.matches(expected.as_dict()):
                outcome, reason_code, scope = (
                    "failed", "MOVE_IDENTITY_MISMATCH", "global",
                )
                reason = "移动后目标身份与预期源身份不一致（状态不可信）"
            else:
                outcome, reason_code, reason = "moved", "OK", "已移动到目标类别目录"
                dest_identity = actual.as_dict()
        finally:
            for fd in (src_fd, dst_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        result_event = make_event(
            "result",
            op_id,
            outcome=outcome,
            reason_code=reason_code,
            reason=reason,
            scope=scope if outcome != "moved" else None,
        )
        if dest_identity is not None:
            result_event["dest_identity"] = dest_identity
        try:
            journal.append(result_event, report=report)
        except (JournalWriteFailed, JournalCorrupted) as exc:
            result.failed += 1
            result.outcomes.append(
                {
                    "op_id": op_id,
                    "outcome": "failed",
                    "reason_code": "LOG_WRITE_FAILED",
                    "reason": f"移动结果未能落盘（该文件可能已移动，需 recover 判定）：{exc}",
                }
            )
            result.stopped = True
            log_failure = True
            result.stop_reason = "移动结果未能落盘（moved_unrecorded）"
            write_plan_state(settings, plan_id, phase=PHASE_ABORTED_LOG_FAILURE, last_error=str(exc))
            stopped = True
            stop_reason = result.stop_reason
            continue

        result.outcomes.append(
            {
                "op_id": op_id,
                "outcome": outcome,
                "reason_code": reason_code,
                "reason": reason,
                "scope": scope,
            }
        )
        if outcome == "moved":
            result.moved += 1
        elif outcome == "skipped":
            result.skipped += 1
        else:
            result.failed += 1
        if outcome != "moved" and scope == "global":
            result.stopped = True
            result.stop_reason = f"{reason_code}: {reason}"
            stopped = True
            stop_reason = result.stop_reason
        write_plan_state(
            settings,
            plan_id,
            phase=PHASE_EXECUTING,
            last_op_id=op_id,
            counts={
                "total": len(operations),
                "moved": result.moved,
                "skipped": result.skipped,
                "failed": result.failed,
                "not_attempted": result.not_attempted,
            },
        )

    # 恢复悬空项（补记确定结果），再重建撤销快照
    recovery = recover_execution(settings, plan_id, lock=lock, kind="cli")
    result.recovery_notes.extend(recovery.get("notes", []))

    try:
        fold, undo_fold = fold_verified_journals(settings.state_dir, plan_id)
    except JournalNeedsRepair as exc:
        # 尚需一次安全修复（合法状态）：置 needs_review 并保留证据，绝不重建或覆盖
        detail = exc.state.detail if exc.state else str(exc)
        write_plan_state(settings, plan_id, phase=PHASE_NEEDS_REVIEW, last_error=detail)
        result.stopped = True
        result.stop_reason = f"日志尚需一次安全修复（请运行 recover）：{detail}"
        result.recovery_notes.append(result.stop_reason)
        result.phase = PHASE_NEEDS_REVIEW
        return result
    except JournalCorrupted as exc:
        # 任一日志损坏 → 计划置 corrupted、保留旧证据与快照、绝不重建或覆盖
        write_plan_state(
            settings, plan_id, phase=PHASE_CORRUPTED, last_error=str(exc)
        )
        result.stopped = True
        result.stop_reason = f"日志损坏（corrupted）：{exc}"
        result.recovery_notes.append(result.stop_reason)
        result.phase = PHASE_CORRUPTED
        return result
    snapshot = derive_snapshot(plan=plan, execution_fold=fold, undo_fold=undo_fold)
    write_snapshot(settings, plan_id, snapshot)

    prior_phase = current_phase(settings, plan_id)
    if prior_phase in TERMINAL_ABNORMAL_PHASES:
        # 已有 corrupted / needs_review / aborted_* 等异常状态：
        # 收尾**不得**把它覆盖成 executed（P1 修复）
        phase = prior_phase
    elif log_failure:
        phase = PHASE_ABORTED_LOG_FAILURE
    elif any(o.get("scope") == "global" and o["outcome"] == "failed" for o in result.outcomes):
        # global 失败（含 MOVE_IDENTITY_MISMATCH）：终态必须是异常态，
        # 不得留在 executing，也不得写成 executed（P1 修复）
        phase = PHASE_NEEDS_REVIEW
    else:
        phase = PHASE_EXECUTED
    write_plan_state(
        settings,
        plan_id,
        phase=phase,
        counts={
            "total": len(operations),
            "moved": result.moved,
            "skipped": result.skipped,
            "failed": result.failed,
            "not_attempted": result.not_attempted,
        },
        undo_status=snapshot.get("undo_status"),
    )
    result.phase = phase
    return result


# --- 恢复 ---------------------------------------------------------------------
def recover_execution(
    settings: Settings,
    plan_id: str,
    *,
    lock: GlobalLock | None = None,
    kind: str = "cli",
    write: bool = True,
) -> dict[str, Any]:
    """恢复执行：绑定配置 → 无副作用双日志准入 → 封存/修复 → 补记 → 重建快照。

    顺序是硬要求（P1 修复）：

    1. 取锁（失败即拒绝，**不做任何其他动作**）；
    2. 绑定当前配置并校验路径/身份（``verify_binding=True``）；
    3. **只读**校验两份日志：损坏 → 记录 ``corrupted``、**零补写、零封存、零快照替换**；
    4. 通过后才允许封存截断尾行、修复清单发布、补记结果、重建快照；
    5. 只允许「补写已完成项」，**不允许自动重试移动**。
    """
    own_lock = lock is None
    lock = lock or GlobalLock(settings)
    handle = None
    if own_lock:
        try:
            handle = lock.acquire(kind=kind, action="recover", plan_id=plan_id)
        except LockBusy as exc:
            raise ExecutionRefused(str(exc)) from exc
    try:
        plan = load_verified_plan(settings, plan_id, verify_binding=True)
        journal = _journal(settings, plan_id)
        report = ReadReport()
        notes: list[str] = []
        recovered: list[str] = []
        unknown: list[str] = []

        # 步骤 3：无副作用准入（**两份**日志都必须 healthy 或 repairable）
        try:
            classified = assert_journals_repairable(settings, plan_id, action="恢复")
        except ExecutionRefused as exc:
            write_plan_state(settings, plan_id, phase=PHASE_CORRUPTED, last_error=str(exc))
            return {
                "plan_id": plan_id,
                "phase": PHASE_CORRUPTED,
                "notes": [f"{exc}；已保留旧证据，未补写、未封存、未替换快照"],
                "recovered": [],
                "unknown": [],
                "truncated": None,
                "undo_status": None,
            }
        notes.extend(
            f"{item['label']}：{item['state'].detail}"
            for item in classified.values()
            if item["state"].repairable
        )

        # 步骤 4：两份都已验证 → 才执行明确的修复（幂等）
        try:
            journal.repair_if_needed(report=report)
            Journal(settings.state_dir, plan_id, "undo").repair_if_needed(report=report)
        except JournalCorrupted as exc:
            write_plan_state(settings, plan_id, phase=PHASE_CORRUPTED, last_error=str(exc))
            return {
                "plan_id": plan_id,
                "phase": PHASE_CORRUPTED,
                "notes": [str(exc)],
                "recovered": [],
                "unknown": [],
                "truncated": report.as_dict(),
                "undo_status": None,
            }
        except JournalWriteFailed as exc:
            write_plan_state(settings, plan_id, phase=PHASE_ABORTED_LOG_FAILURE, last_error=str(exc))
            return {
                "plan_id": plan_id,
                "phase": PHASE_ABORTED_LOG_FAILURE,
                "notes": [f"封存/修复失败（日志不可靠，拒绝继续）：{exc}"],
                "recovered": [],
                "unknown": [],
                "truncated": report.as_dict(),
                "undo_status": None,
            }
        notes.extend(report.notes)

        fold, _ = journal.fold(allow_repair=True)
        for operation in plan.get("operations", []):
            op_id = str(operation["op_id"])
            slot = fold.get(op_id, {})
            intent = slot.get("intent")
            if intent is None or slot.get("result") is not None:
                continue
            source = str(operation["source"])
            dest = str(operation["dest"])
            expected = intent.get("identity") or operation["identity"]
            try:
                source_identity = lstat_identity(source)
                dest_identity = lstat_identity(dest)
            except OSError as exc:
                unknown.append(op_id)
                notes.append(f"{op_id}: 无法判定（{exc}），需人工检查")
                continue

            if source_identity is None and dest_identity is not None and dest_identity.matches(expected):
                # 移动已完成，只是 result 未落盘 → 补记（基准取 intent 的 identity）
                if write:
                    try:
                        journal.append(
                            make_event(
                                "result",
                                op_id,
                                outcome="moved",
                                reason_code="RECOVERED_FROM_INTENT",
                                reason="恢复：目标身份与 intent 预期身份完整匹配，补记移动已完成",
                                dest_identity=dict(expected),
                            ),
                            report=report,
                        )
                    except JournalWriteFailed as exc:
                        write_plan_state(
                            settings, plan_id, phase=PHASE_ABORTED_LOG_FAILURE, last_error=str(exc)
                        )
                        return {
                            "plan_id": plan_id,
                            "phase": PHASE_ABORTED_LOG_FAILURE,
                            "notes": [f"补写失败，已停止（日志不可靠）：{exc}"],
                            "recovered": recovered,
                            "unknown": unknown,
                            "truncated": report.as_dict(),
                            "undo_status": None,
                        }
                recovered.append(op_id)
            elif source_identity is not None and dest_identity is None:
                notes.append(f"{op_id}: 未执行（源仍在、目标不存在）→ 标 not_attempted，不自动重试")
            else:
                unknown.append(op_id)
                notes.append(f"{op_id}: 状态不可判，需人工检查（绝不移动）")

        try:
            fold, undo_fold = fold_verified_journals(settings.state_dir, plan_id)
        except JournalCorrupted as exc:
            write_plan_state(settings, plan_id, phase=PHASE_CORRUPTED, last_error=str(exc))
            return {
                "plan_id": plan_id,
                "phase": PHASE_CORRUPTED,
                "notes": [f"日志损坏（corrupted）：{exc}；已保留旧证据，不重建快照"],
                "recovered": recovered,
                "unknown": unknown,
                "truncated": report.as_dict(),
                "undo_status": None,
            }
        snapshot = derive_snapshot(plan=plan, execution_fold=fold, undo_fold=undo_fold)
        if write:
            write_snapshot(settings, plan_id, snapshot)
            if unknown:
                write_plan_state(settings, plan_id, phase=PHASE_NEEDS_REVIEW)
        phase = PHASE_NEEDS_REVIEW if unknown else current_phase(settings, plan_id)
        return {
            "plan_id": plan_id,
            "phase": phase,
            "notes": notes,
            "recovered": recovered,
            "unknown": unknown,
            "truncated": report.as_dict(),
            "undo_status": snapshot.get("undo_status"),
        }
    finally:
        if own_lock:
            lock.release(handle)


def execution_summary(result: ExecutionResult) -> str:
    lines = [
        f"计划 {result.plan_id}：成功 {result.moved} · 跳过 {result.skipped} · "
        f"失败 {result.failed} · 未处理 {result.not_attempted}"
    ]
    if result.stopped and result.stop_reason:
        lines.append(f"已停止：{result.stop_reason}")
        lines.append(f"其余 {result.not_attempted} 项未处理")
    for item in result.outcomes:
        mark = {"moved": "✓", "skipped": "→", "failed": "✗"}.get(item["outcome"], "?")
        lines.append(f"  {mark} {item['op_id']} {item['reason_code']} {item['reason']}")
    return "\n".join(lines)
