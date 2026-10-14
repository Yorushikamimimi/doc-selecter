"""撤销快照的**推导**与读写。

职责边界（docs/MVP方案.md §5.5.3 / v3.1 收口）：

- 快照是**派生**数据，不是真相来源；真相来源是执行日志 + 撤销日志。
- 推导时**身份基准只能来自持久化记录**：有成功 ``result`` 用其 ``dest_identity``；
  悬空项用 ``intent.identity``（且必须五项完整匹配才承认移动发生过）。
  **绝不允许**用推导时刻 ``lstat`` 到的当前值建立新基准。
- 已有撤销事件（``undo_skip`` / ``undo_intent`` / ``undo_result``）必须**保留**，
  不能一律重置为 ``pending``；推导过程**不写任何日志事件**（尤其不写 ``undo_intent``）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping

from .atomic_move import lstat_identity, matches_identity
from .config import Settings

# --- 条目状态（互斥单一取值，§5.4） -------------------------------------------
UNDO_PENDING = "pending"
UNDO_ATTEMPTING = "attempting"
UNDO_RESTORED = "restored"
UNDO_BLOCKED_SOURCE_OCCUPIED = "blocked_source_occupied"
UNDO_BLOCKED_DEST_CHANGED = "blocked_dest_changed"
UNDO_BLOCKED_DEST_MISSING = "blocked_dest_missing"
UNDO_BLOCKED_DIR_CHANGED = "blocked_dir_changed"
UNDO_SKIPPED_NOT_MOVED = "skipped_not_moved"
UNDO_UNKNOWN = "unknown_needs_review"

#: 终态（不再尝试还原）
TERMINAL_UNDO_STATES = frozenset(
    {
        UNDO_RESTORED,
        UNDO_BLOCKED_SOURCE_OCCUPIED,
        UNDO_BLOCKED_DEST_CHANGED,
        UNDO_BLOCKED_DEST_MISSING,
        UNDO_BLOCKED_DIR_CHANGED,
        UNDO_SKIPPED_NOT_MOVED,
        UNDO_UNKNOWN,
    }
)
#: local 冲突（跳过该项并继续）
LOCAL_UNDO_STATES = frozenset(
    {
        UNDO_BLOCKED_SOURCE_OCCUPIED,
        UNDO_BLOCKED_DEST_CHANGED,
        UNDO_BLOCKED_DEST_MISSING,
    }
)
#: global 问题（停止整批）
GLOBAL_UNDO_STATES = frozenset({UNDO_BLOCKED_DIR_CHANGED, UNDO_UNKNOWN})

# --- 整批状态 -----------------------------------------------------------------
UNDO_STATUS_NOT_STARTED = "not_started"
UNDO_STATUS_IN_PROGRESS = "in_progress"
UNDO_STATUS_COMPLETED = "completed"
UNDO_STATUS_PARTIAL = "partial_undo"
UNDO_STATUS_ABORTED_GLOBAL = "aborted_global"
UNDO_STATUS_ABORTED_LOG_FAILURE = "aborted_log_failure"
UNDO_STATUS_NEEDS_REVIEW = "needs_review"


def fold_verified_journals(
    state_dir: str, plan_id: str
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """先校验**两份**日志，再返回折叠结果。

    P1 修复：调用方**不得**把 :class:`JournalCorrupted` 吞成「空撤销历史」。
    任一日志损坏都必须向上传播，由调用方把计划置为 ``corrupted`` 并停止，
    同时保留旧证据与快照（不重建、不覆盖）。

    Raises:
        JournalCorrupted: 执行日志或撤销日志损坏。
    """
    from .journal import Journal

    execution_fold, _ = Journal(state_dir, plan_id, "execution").fold(allow_repair=False)
    undo_fold, _ = Journal(state_dir, plan_id, "undo").fold(allow_repair=False)
    return execution_fold, undo_fold


def snapshot_path(settings: Settings, plan_id: str) -> str:
    return os.path.join(settings.undos_dir, f"undo-{plan_id}.json")


def write_snapshot(settings: Settings, plan_id: str, snapshot: Mapping[str, Any]) -> None:
    """原子写入快照（临时文件 + ``os.replace``）。绝不写日志事件。"""
    os.makedirs(settings.undos_dir, mode=0o700, exist_ok=True)
    path = snapshot_path(settings, plan_id)
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def read_snapshot(settings: Settings, plan_id: str) -> dict[str, Any] | None:
    path = snapshot_path(settings, plan_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def derive_snapshot(
    *,
    plan: Mapping[str, Any],
    execution_fold: Mapping[str, Mapping[str, Any]],
    undo_fold: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """从执行日志（+ 已有撤销日志）推导撤销快照。

    ``execution_fold`` / ``undo_fold`` 来自 :class:`journal.Journal.fold`。
    """
    undo_fold = undo_fold or {}
    entries: list[dict[str, Any]] = []
    global_stop = False

    for operation in plan.get("operations", []):
        op_id = str(operation["op_id"])
        slot = execution_fold.get(op_id, {})
        result = slot.get("result")
        outcome = str(result.get("outcome")) if result else None
        undo_slot = undo_fold.get(op_id, {})

        if outcome != "moved":
            # 从未成功移动 → 无需还原
            entry = _base_entry(operation)
            entry["undo_state"] = UNDO_SKIPPED_NOT_MOVED
            entry["undo_reason"] = f"执行结果：{outcome or 'not_attempted'}"
            entries.append(entry)
            continue

        # 身份基准：**只能**来自持久化记录
        dest_identity = result.get("dest_identity")
        identity_source = "result"
        if not isinstance(dest_identity, dict):
            intent = slot.get("intent") or {}
            dest_identity = intent.get("identity")
            identity_source = "intent"
        if not isinstance(dest_identity, dict):
            entry = _base_entry(operation)
            entry["undo_state"] = UNDO_UNKNOWN
            entry["undo_reason"] = "缺少持久化身份基准，无法建立可撤销条目"
            global_stop = True
            entries.append(entry)
            continue

        entry = _base_entry(operation)
        entry["dest_identity"] = _five(dest_identity)
        entry["identity_source"] = identity_source
        entry["undo_attempts"] = int(undo_slot.get("undo_result", {}).get("undo_attempts", 0)) if undo_slot.get("undo_result") else 0

        prior = _prior_undo_state(undo_slot)
        if prior is not None:
            entry["undo_state"] = prior[0]
            if prior[1]:
                entry["undo_reason"] = prior[1]
            if entry["undo_state"] in GLOBAL_UNDO_STATES:
                global_stop = True
            entries.append(entry)
            continue

        # 尚未尝试还原：物化 pending，但若当前目标已与基准不符，则直接物化 blocked
        current = lstat_identity(str(operation["dest"]))
        if current is None:
            entry["undo_state"] = UNDO_BLOCKED_DEST_MISSING
            entry["undo_reason"] = "目标已不在（被移走或删除）"
        elif not matches_identity(str(operation["dest"]), entry["dest_identity"]):
            entry["undo_state"] = UNDO_BLOCKED_DEST_CHANGED
            entry["undo_reason"] = "目标当前身份与持久化基准不一致（已被改动）"
        else:
            entry["undo_state"] = UNDO_PENDING
        entries.append(entry)

    snapshot = {
        "schema_version": 1,
        "plan_id": str(plan.get("plan_id")),
        "plan_hash": plan.get("plan_hash"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_of_truth": "journal gen-*.jsonl + undo gen-*.jsonl",
        "identity_basis": "persisted_result_dest_identity | persisted_intent_identity",
        "entries": entries,
        "undo_status": _aggregate_status(entries, global_stop),
    }
    if plan.get("schema_version") == 3:
        from .directory_archive import directory_snapshot
        snapshot["schema_version"] = 2
        snapshot["directory_entries"] = directory_snapshot(plan, execution_fold, undo_fold)
        for op,entry in zip(plan["operations"], entries):
            slot = execution_fold.get(op["op_id"], {})
            if slot.get("intent") and (not slot.get("result") or slot["result"].get("reason_code") in {"MOVE_IDENTITY_MISMATCH","UNKNOWN_ERROR"}):
                entry["undo_state"] = UNDO_UNKNOWN
        snapshot["undo_status"] = _aggregate_status(entries + snapshot["directory_entries"],
            any(e["undo_state"] in GLOBAL_UNDO_STATES for e in entries + snapshot["directory_entries"]))
    return snapshot


def _base_entry(operation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "op_id": str(operation["op_id"]),
        "original_source": str(operation["source"]),
        "current_dest": str(operation["dest"]),
        "category": operation.get("category"),
        "dest_identity": None,
        "identity_source": None,
        "undo_state": UNDO_SKIPPED_NOT_MOVED,
        "undo_attempts": 0,
        "undo_reason": None,
    }


def _five(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "st_dev": int(identity["st_dev"]),
        "st_ino": int(identity["st_ino"]),
        "size_bytes": int(identity["size_bytes"]),
        "mtime_ns": int(identity["mtime_ns"]),
        "mode_type": str(identity["mode_type"]),
    }


def _prior_undo_state(undo_slot: Mapping[str, Any]) -> tuple[str, str | None] | None:
    """把已有撤销事件映射为条目状态；没有则返回 ``None``。"""
    result = undo_slot.get("undo_result")
    if result is not None:
        reason_code = str(result.get("reason_code", ""))
        outcome = str(result.get("outcome", ""))
        if outcome == "restored" or reason_code == "OK":
            return UNDO_RESTORED, None
        state = result.get("undo_state")
        if isinstance(state, str) and state in TERMINAL_UNDO_STATES:
            return state, str(result.get("reason") or "")
        # 由 reason_code 反推
        mapped = {
            "DEST_EXISTS": UNDO_BLOCKED_SOURCE_OCCUPIED,
            "DEST_CHANGED": UNDO_BLOCKED_DEST_CHANGED,
            "DEST_MISSING": UNDO_BLOCKED_DEST_MISSING,
            "DIR_IDENTITY_CHANGED": UNDO_BLOCKED_DIR_CHANGED,
            "UNKNOWN_STATE": UNDO_UNKNOWN,
            "PERMISSION_DENIED": UNDO_UNKNOWN,
        }.get(reason_code)
        if mapped:
            return mapped, str(result.get("reason") or "")
        return UNDO_UNKNOWN, f"撤销结果无法归类：{reason_code}"
    if undo_slot.get("undo_intent") is not None:
        return UNDO_ATTEMPTING, None
    skip = undo_slot.get("undo_skip")
    if skip is not None:
        state = str(skip.get("undo_state", ""))
        if state in TERMINAL_UNDO_STATES:
            return state, str(skip.get("reason") or "")
        return UNDO_UNKNOWN, "undo_skip 终态无法归类"
    return None


def _aggregate_status(entries: list[Mapping[str, Any]], global_stop: bool) -> str:
    if global_stop:
        return UNDO_STATUS_NEEDS_REVIEW
    relevant = [e for e in entries if e["undo_state"] != UNDO_SKIPPED_NOT_MOVED]
    if not relevant:
        return UNDO_STATUS_NOT_STARTED
    if all(e["undo_state"] == UNDO_RESTORED for e in relevant):
        return UNDO_STATUS_COMPLETED
    if any(e["undo_state"] == UNDO_PENDING or e["undo_state"] == UNDO_ATTEMPTING for e in relevant):
        return UNDO_STATUS_IN_PROGRESS
    return UNDO_STATUS_PARTIAL


def recompute_aggregate(snapshot: dict[str, Any]) -> str:
    global_stop = any(e.get("undo_state") in GLOBAL_UNDO_STATES for e in snapshot.get("entries", []))
    status = _aggregate_status(snapshot.get("entries", []), global_stop)
    snapshot["undo_status"] = status
    return status
