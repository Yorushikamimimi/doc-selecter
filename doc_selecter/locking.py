"""全局排他锁与计划执行状态（GUI / CLI 共用）。

- 锁：``state_dir/lock`` 上的 ``fcntl.flock(LOCK_EX | LOCK_NB)``，由内核持有，
  进程退出/崩溃时自动释放（不留死锁）；拿不到锁即**拒绝启动**执行或撤销。
- 计划状态：``state_dir/plan_state/<plan_id>.json``，在锁内读改写，用
  「临时文件 + ``os.replace``」原子替换。
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings


class LockBusy(RuntimeError):
    """另一个实例正在运行。"""


# --- 计划阶段 -----------------------------------------------------------------
PHASE_PREPARED = "prepared"
PHASE_LOCKED = "locked"
PHASE_EXECUTING = "executing"
PHASE_EXECUTED = "executed"
PHASE_ABORTED_LOG_FAILURE = "aborted_log_failure"
PHASE_UNKNOWN_NEEDS_REVIEW = "unknown_needs_review"
PHASE_CORRUPTED = "corrupted"
PHASE_UNDOING = "undoing"
PHASE_UNDONE = "undone"
PHASE_PARTIAL_UNDO = "partial_undo"
PHASE_NEEDS_REVIEW = "needs_review"
PHASE_ABORTED_GLOBAL = "aborted_global"

#: 在这些阶段禁止开始新的执行（§5.6.3 准入规则 2）
EXECUTE_BLOCKED_PHASES = frozenset(
    {
        PHASE_EXECUTING,
        PHASE_EXECUTED,
        PHASE_UNDOING,
        PHASE_PARTIAL_UNDO,
        PHASE_UNDONE,
        PHASE_ABORTED_LOG_FAILURE,
        PHASE_UNKNOWN_NEEDS_REVIEW,
        PHASE_CORRUPTED,
        PHASE_NEEDS_REVIEW,
    }
)
#: 在这些阶段禁止开始撤销
UNDO_BLOCKED_PHASES = frozenset({PHASE_UNDOING, PHASE_CORRUPTED})

#: 异常终止阶段：执行收尾**不得**把这些状态覆盖成 executed（P1 修复）
TERMINAL_ABNORMAL_PHASES = frozenset(
    {
        PHASE_CORRUPTED,
        PHASE_NEEDS_REVIEW,
        PHASE_ABORTED_LOG_FAILURE,
        PHASE_ABORTED_GLOBAL,
    }
)


@dataclass
class LockHandle:
    path: str
    fd: int
    holder: dict[str, Any]


class GlobalLock:
    """``state_dir/lock`` 上的全局排他锁。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.lock_path
        self._fd: int | None = None

    def acquire(self, *, kind: str, action: str, plan_id: str | None = None) -> LockHandle:
        os.makedirs(self.settings.state_dir, mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = _read_holder(fd)
            os.close(fd)
            who = _format_holder(holder)
            raise LockBusy(
                f"另一个实例正在运行（{who}）。请等待其完成或关闭该窗口。"
            ) from exc
        holder = {
            "pid": os.getpid(),
            "kind": kind,
            "action": action,
            "plan_id": plan_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        try:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(holder, ensure_ascii=False).encode("utf-8"))
            os.fsync(fd)
        except OSError:
            pass
        self._fd = fd
        return LockHandle(path=self.path, fd=fd, holder=holder)

    def release(self, handle: LockHandle | None = None) -> None:
        fd = handle.fd if handle is not None else self._fd
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        self._fd = None


def _read_holder(fd: int) -> dict[str, Any]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096)
        data = json.loads(raw.decode("utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _format_holder(holder: dict[str, Any]) -> str:
    if not holder:
        return "持有者信息不可读"
    return (
        f"pid={holder.get('pid')}，动作={holder.get('action')}，"
        f"类型={holder.get('kind')}，计划={holder.get('plan_id')}"
    )


# --- 计划状态 -----------------------------------------------------------------
def plan_state_path(settings: Settings, plan_id: str) -> str:
    return os.path.join(settings.plan_state_dir, f"{plan_id}.json")


def read_plan_state(settings: Settings, plan_id: str) -> dict[str, Any]:
    path = plan_state_path(settings, plan_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_plan_state(settings: Settings, plan_id: str, **fields: Any) -> dict[str, Any]:
    """在锁内读改写；用临时文件 + ``os.replace`` 原子替换。"""
    os.makedirs(settings.plan_state_dir, mode=0o700, exist_ok=True)
    state = read_plan_state(settings, plan_id)
    state.update(fields)
    state.setdefault("schema_version", 1)
    state["plan_id"] = plan_id
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path = plan_state_path(settings, plan_id)
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return state


def current_phase(settings: Settings, plan_id: str) -> str:
    return str(read_plan_state(settings, plan_id).get("phase") or PHASE_PREPARED)
