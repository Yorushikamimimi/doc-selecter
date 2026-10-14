"""执行日志 / 撤销日志：代次封存（generation sealing）+ 清单（manifest）。

契约见 docs/MVP方案.md §5.3.2.1 / §5.3.3：

- 日志按**代次文件**存放：``<kind>-<plan_id>.gen-0001.jsonl``；
- 每份计划、每种日志各有一份**独立清单** ``<kind>-<plan_id>.manifest.json``；
- 旧代次**永不改动**：封存时记录 ``usable_bytes`` / ``file_bytes`` / ``crc32_full``；
- 末行截断时才封存并开新代次；中间损坏 → ``corrupted``，拒绝继续且**不封存**；
- 事件白名单按日志种类分开（执行：``intent``/``result``；撤销：``undo_intent``/
  ``undo_result``/``undo_skip``），禁止交叉。

**幂等性**：所有恢复动作（重建清单、完成被中断的清单发布、封存）都可重复执行，
第二次执行结果与第一次相同，不产生新的分支或重复事件。
"""

from __future__ import annotations

import json
import os
import stat as statmod
import time
import unicodedata
import zlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from .plan_hash import canonical_json

EXECUTION_EVENTS = frozenset({"intent", "result"})
UNDO_EVENTS = frozenset({"undo_intent", "undo_result", "undo_skip"})

LOG_KIND_EVENTS = {"execution": EXECUTION_EVENTS, "undo": UNDO_EVENTS}
LOG_KIND_PREFIX = {"execution": "journal", "undo": "undo"}
LOG_KIND_DIR = {"execution": "journals", "undo": "undos"}


class JournalCorrupted(RuntimeError):
    """日志损坏（中间行不完整、CRC 失配、事件非法、代次连接缺失）。"""


class JournalNeedsRepair(JournalCorrupted):
    """日志**健康**但需要一次明确的、方案允许的修复（清单重建 / 完成发布）。

    与 :class:`JournalCorrupted` 区分：本异常只表示「尚未修复」，
    恢复入口可以安全修复；执行/撤销入口必须拒绝并提示 ``recover``。
    """

    def __init__(self, message: str, state: "JournalState | None" = None) -> None:
        super().__init__(message)
        self.state = state


class JournalWriteFailed(RuntimeError):
    """日志写入或 fsync 失败 → 调用方必须立即停止，绝不继续移动。"""


JOURNAL_HEALTHY = "healthy"
JOURNAL_REPAIRABLE = "repairable"
JOURNAL_CORRUPTED = "corrupted"


@dataclass
class JournalState:
    """结构化日志状态：``classification`` ∈ {healthy, repairable, corrupted}。

    ``repair_kind``：``None`` / ``"rebuild_manifest_single_generation"`` /
    ``"register_pending_generation"`` / ``"seal_truncated_tail"``。
    ``truncated`` 表示已登记活动代次存在不完整尾行（合法截断，可封存）。
    """

    classification: str
    detail: str = ""
    repair_kind: str | None = None
    truncated: bool = False
    truncated_op_id: str | None = None
    truncated_bytes: int = 0
    pending_gen: int | None = None
    pending_file: str | None = None
    generations: int = 0

    @property
    def healthy(self) -> bool:
        return self.classification == JOURNAL_HEALTHY

    @property
    def repairable(self) -> bool:
        return self.classification == JOURNAL_REPAIRABLE

    @property
    def corrupted(self) -> bool:
        return self.classification == JOURNAL_CORRUPTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "detail": self.detail,
            "repair_kind": self.repair_kind,
            "truncated": self.truncated,
            "truncated_op_id": self.truncated_op_id,
            "truncated_bytes": self.truncated_bytes,
            "pending_gen": self.pending_gen,
            "pending_file": self.pending_file,
            "generations": self.generations,
        }


@dataclass
class Generation:
    gen: int
    file: str
    status: str
    sealed_reason: str | None = None
    usable_bytes: int | None = None
    file_bytes: int | None = None
    truncated_bytes: int | None = None
    crc32_full: str | None = None
    sealed_at: str | None = None
    opened_at: str | None = None


@dataclass
class ReadReport:
    truncated_tail_line: str | None = None
    truncated_op_id: str | None = None
    truncated_bytes: int = 0
    sealed_now: list[int] = field(default_factory=list)
    manifest_rebuilt: bool = False
    repaired_publish: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "truncated_tail_line": self.truncated_tail_line,
            "truncated_op_id": self.truncated_op_id,
            "truncated_bytes": self.truncated_bytes,
            "sealed_now": list(self.sealed_now),
            "manifest_rebuilt": self.manifest_rebuilt,
            "repaired_publish": self.repaired_publish,
            "notes": list(self.notes),
        }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def crc32_hex(data: bytes) -> str:
    return format(zlib.crc32(data) & 0xFFFFFFFF, "08x")


def line_crc32(payload: Mapping[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "crc32"}
    return crc32_hex(canonical_json(body).encode("utf-8"))


def _read_bytes(path: str) -> bytes:
    """读取文件字节。

    只有 ``ENOENT`` 会被调用方解释为「文件不存在」；**权限与 I/O 错误一律抛出**
    （``JournalCorrupted``），绝不能被当成「不存在」而放行。
    """
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise JournalCorrupted(f"无法读取代次文件（权限/I/O 错误不得当作不存在）：{path}（{exc}）") from exc


def _peek_op_id(tail: bytes) -> str | None:
    text = tail.decode("utf-8", "replace")
    idx = text.find('"op_id"')
    if idx == -1:
        return None
    colon = text.find(":", idx)
    if colon == -1:
        return None
    start = text.find('"', colon)
    if start == -1:
        return None
    end = text.find('"', start + 1)
    return text[start + 1:end] if end != -1 else None


class Journal:
    """一种日志（执行或撤销）的代次管理器。"""

    def __init__(self, state_dir: str, plan_id: str, log_kind: str) -> None:
        if log_kind not in LOG_KIND_EVENTS:
            raise ValueError(f"未知日志种类：{log_kind}")
        self.log_kind = log_kind
        self.plan_id = plan_id
        self.allowed_events = LOG_KIND_EVENTS[log_kind]
        self.prefix = LOG_KIND_PREFIX[log_kind]
        self.dir = os.path.join(state_dir, LOG_KIND_DIR[log_kind])
        self.manifest_path = os.path.join(self.dir, f"{self.prefix}-{plan_id}.manifest.json")

    # --- 路径 ---------------------------------------------------------------
    def generation_file(self, gen: int) -> str:
        return os.path.join(self.dir, f"{self.prefix}-{self.plan_id}.gen-{gen:04d}.jsonl")

    def generation_name(self, gen: int) -> str:
        return f"{self.prefix}-{self.plan_id}.gen-{gen:04d}.jsonl"

    def _existing_generations(self) -> list[tuple[int, str]]:
        """列出代次文件。

        **只读**：绝不创建目录。目录不存在 → 空列表；权限/I/O 错误 → 抛错
        （不得当成「不存在」）。
        """
        try:
            names = os.listdir(self.dir)
        except FileNotFoundError:
            return []
        except NotADirectoryError:
            return []
        except OSError as exc:
            raise JournalCorrupted(
                f"无法列出日志目录（权限/I/O 错误不得当作不存在）：{self.dir}（{exc}）"
            ) from exc
        marker = f"{self.prefix}-{self.plan_id}.gen-"
        found: list[tuple[int, str]] = []
        for name in names:
            if name.startswith(marker) and name.endswith(".jsonl"):
                middle = name[len(marker):-len(".jsonl")]
                if middle.isdigit():
                    found.append((int(middle), name))
        return sorted(found)

    # --- 清单 ---------------------------------------------------------------
    def _load_manifest(self) -> dict[str, Any] | None:
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise JournalCorrupted(f"清单无法读取或不是合法 JSON：{self.manifest_path}（{exc}）") from exc
        if not isinstance(data, dict):
            raise JournalCorrupted(f"清单顶层必须是 JSON 对象：{self.manifest_path}")
        if str(data.get("plan_id")) != self.plan_id or data.get("log_kind") != self.log_kind:
            raise JournalCorrupted("清单内容与请求的计划/日志种类不一致")
        return data

    def _write_manifest(self, manifest: Mapping[str, Any]) -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        tmp = f"{self.manifest_path}.tmp-{os.getpid()}"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.manifest_path)

    def _manifest_dict(self, generations: list[Generation], active_gen: int | None) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for generation in generations:
            if generation.status == "sealed":
                items.append(
                    {
                        "gen": generation.gen,
                        "file": generation.file,
                        "status": "sealed",
                        "sealed_reason": generation.sealed_reason,
                        "usable_bytes": generation.usable_bytes,
                        "file_bytes": generation.file_bytes,
                        "truncated_bytes": generation.truncated_bytes,
                        "crc32_full": generation.crc32_full,
                        "sealed_at": generation.sealed_at or _now(),
                    }
                )
            else:
                items.append(
                    {
                        "gen": generation.gen,
                        "file": generation.file,
                        "status": "active",
                        "opened_at": generation.opened_at or _now(),
                    }
                )
        return {
            "schema_version": 1,
            "plan_id": self.plan_id,
            "log_kind": self.log_kind,
            "active_gen": active_gen,
            "generations": items,
        }

    def _generations_from_manifest(self, manifest: Mapping[str, Any]) -> list[Generation]:
        raw = manifest.get("generations") or []
        if not isinstance(raw, list):
            raise JournalCorrupted("清单 generations 必须是列表")
        out: list[Generation] = []
        for item in raw:
            if not isinstance(item, dict):
                raise JournalCorrupted("清单 generations 元素必须是对象")
            try:
                gen = int(item["gen"])
                file_name = str(item["file"])
            except (KeyError, TypeError, ValueError) as exc:
                raise JournalCorrupted("清单 generations 元素缺少 gen/file") from exc
            out.append(
                Generation(
                    gen=gen,
                    file=file_name,
                    status=str(item.get("status", "active")),
                    sealed_reason=item.get("sealed_reason"),
                    usable_bytes=item.get("usable_bytes"),
                    file_bytes=item.get("file_bytes"),
                    truncated_bytes=item.get("truncated_bytes"),
                    crc32_full=item.get("crc32_full"),
                    sealed_at=item.get("sealed_at"),
                    opened_at=item.get("opened_at"),
                )
            )
        out.sort(key=lambda g: g.gen)
        return out

    # --- 解析 ---------------------------------------------------------------
    def _split_complete_lines(self, data: bytes) -> tuple[list[bytes], bytes]:
        """按 ``\\n`` 切分，返回 ``(完整行, 末尾不完整字节)``。

        中间的空行会保留下来交给解析器判为损坏（每一行都必须是完整 JSON）。
        """
        if not data:
            return [], b""
        lines = data.split(b"\n")
        tail = lines.pop()
        return lines, tail

    def _parse_line(self, raw: bytes, *, where: str) -> dict[str, Any]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JournalCorrupted(f"{where} 不是合法 UTF-8") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JournalCorrupted(f"{where} 不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise JournalCorrupted(f"{where} 必须是 JSON 对象")
        event = payload.get("event")
        if event not in self.allowed_events:
            raise JournalCorrupted(
                f"{where} 的事件 {event!r} 不属于 {self.log_kind} 日志的合法事件集合 "
                f"{sorted(self.allowed_events)}"
            )
        op_id = payload.get("op_id")
        if not isinstance(op_id, str) or not op_id:
            raise JournalCorrupted(f"{where} 缺少 op_id")
        stored_crc = payload.get("crc32")
        if stored_crc is not None and (
            not isinstance(stored_crc, str) or stored_crc != line_crc32(payload)
        ):
            raise JournalCorrupted(f"{where} crc32 校验失败")
        if str(payload.get("plan_id", self.plan_id)) != self.plan_id:
            raise JournalCorrupted(f"{where} 的 plan_id 与清单不一致")
        return payload

    def _validate_events(self, events: list[dict[str, Any]], *, where: str) -> None:
        seen: dict[str, dict[str, int]] = {}
        for payload in events:
            op_id = str(payload["op_id"])
            slot = seen.setdefault(op_id, {})
            event = str(payload["event"])
            slot[event] = slot.get(event, 0) + 1
            if slot[event] > 1:
                raise JournalCorrupted(f"{where} 中 {op_id} 出现重复的 {event} 事件")
            if self.log_kind == "execution":
                if event == "result" and slot.get("intent", 0) == 0:
                    raise JournalCorrupted(f"{where} 中 {op_id} 出现无在先 intent 的孤立 result")
            else:
                if event == "undo_result" and slot.get("undo_intent", 0) == 0:
                    raise JournalCorrupted(
                        f"{where} 中 {op_id} 出现无在先 undo_intent 的孤立 undo_result"
                    )
                if event == "undo_skip":
                    if slot.get("undo_intent", 0) or slot.get("undo_result", 0):
                        raise JournalCorrupted(
                            f"{where} 中 {op_id} 的 undo_skip 与 undo_intent/undo_result 并存"
                        )
                    if not isinstance(payload.get("expected_identity"), dict):
                        raise JournalCorrupted(f"{where} 中 {op_id} 的 undo_skip 缺少 expected_identity")
                    state = str(payload.get("undo_state", ""))
                    if not (state.startswith("blocked_") or state == "unknown_needs_review"):
                        raise JournalCorrupted(f"{where} 中 {op_id} 的 undo_skip 终态非法：{state!r}")

    # --- 代次状态 -----------------------------------------------------------
    # --- 结构化状态分类（真正的只读：不创建目录、不写清单） -----------------
    def classify(self) -> JournalState:
        """**无副作用**地把日志分类为 healthy / repairable / corrupted。

        分类**基于内容验证**，绝不依据异常文本关键词：

        - 目录不存在 → ``healthy``（空日志）；
        - 无代次文件：无清单或空清单 → ``healthy``；清单登记了文件但文件缺失 → ``corrupted``；
        - 有代次文件但无清单：

          * 恰好一个第 1 代且**内容全部合法**（JSON、CRC、事件白名单与配对）
            → ``repairable``（``rebuild_manifest_single_generation``）；
          * 恰好一个第 1 代但内容损坏 → ``corrupted``；
          * 多个代次 → ``corrupted``（无法证明代次连接）；
          * 序号非 1 → ``corrupted``。

        - 有清单：校验登记文件存在、代次号 ``1..N`` 连续、``active_gen`` 与代次一致、
          每行 JSON/CRC/事件合法、除「拼接后最后一行」外无中间损坏；
          活动代次存在不完整尾行属于**合法截断**（``truncated=True``，可封存）；
          未登记文件只允许「恰好 N+1、普通文件、长度为 0」→ ``repairable``
          （``register_pending_generation``）；其余（非空/序号不符/非普通文件/多个）
          → ``corrupted``；已登记代次的长度与 ``crc32_full`` 失配 → ``corrupted``。
        """
        # 1) 目录
        try:
            dir_st = os.lstat(self.dir)
        except FileNotFoundError:
            return JournalState(JOURNAL_HEALTHY, "日志目录不存在（空日志）")
        except OSError as exc:
            return JournalState(
                JOURNAL_CORRUPTED, f"无法访问日志目录（权限/I/O 错误不得当作不存在）：{exc}"
            )
        if statmod.S_ISLNK(dir_st.st_mode):
            return JournalState(JOURNAL_CORRUPTED, f"日志目录是符号链接：{self.dir}")
        if not statmod.S_ISDIR(dir_st.st_mode):
            return JournalState(JOURNAL_CORRUPTED, f"日志路径不是目录：{self.dir}")

        try:
            existing = self._existing_generations()
        except JournalCorrupted as exc:
            return JournalState(JOURNAL_CORRUPTED, str(exc))
        try:
            manifest = self._load_manifest()
        except JournalCorrupted as exc:
            return JournalState(JOURNAL_CORRUPTED, str(exc))

        # 2) 无清单
        if manifest is None:
            if not existing:
                return JournalState(JOURNAL_HEALTHY, "无清单且无代次文件（空日志）")
            if len(existing) > 1:
                return JournalState(
                    JOURNAL_CORRUPTED,
                    "清单丢失且存在多个代次文件，无法证明代次连接关系",
                )
            gen_number, name = existing[0]
            if gen_number != 1:
                return JournalState(JOURNAL_CORRUPTED, "清单丢失且唯一代次文件不是第 1 代")
            try:
                data = _read_bytes(os.path.join(self.dir, name))
                lines, tail = self._split_complete_lines(data)
                events = [
                    self._parse_line(ln, where=f"{name}:{i + 1}") for i, ln in enumerate(lines)
                ]
                self._validate_events(events, where=name)
            except JournalCorrupted as exc:
                return JournalState(JOURNAL_CORRUPTED, f"清单丢失且内容不合法：{exc}")
            return JournalState(
                JOURNAL_REPAIRABLE,
                "单代次清单缺失，内容合法，可重建清单",
                repair_kind="rebuild_manifest_single_generation",
                truncated=bool(tail),
                truncated_op_id=_peek_op_id(tail) if tail else None,
                truncated_bytes=len(tail),
                generations=1,
            )

        # 3) 有清单
        generations = self._generations_from_manifest(manifest)
        numbers = [g.gen for g in generations]
        if numbers != list(range(1, len(numbers) + 1)):
            return JournalState(JOURNAL_CORRUPTED, f"代次连接不完整：{numbers}")
        registered = {g.file for g in generations}
        for generation in generations:
            path = os.path.join(self.dir, generation.file)
            if not os.path.isfile(path) or os.path.islink(path):
                return JournalState(
                    JOURNAL_CORRUPTED, f"清单登记的代次文件缺失或不是普通文件：{generation.file}"
                )
        active_gens = [g for g in generations if g.status == "active"]
        if len(active_gens) > 1:
            return JournalState(JOURNAL_CORRUPTED, "清单中存在多个活动代次")
        active_gen = active_gens[0].gen if active_gens else None
        if manifest.get("active_gen") not in (None, active_gen):
            return JournalState(
                JOURNAL_CORRUPTED,
                f"清单 active_gen={manifest.get('active_gen')!r} 与代次记录 {active_gen!r} 不一致",
            )
        if len(generations) > 1 and active_gen is None:
            return JournalState(JOURNAL_CORRUPTED, "清单有多个代次但缺少活动代次")

        # 3a) 逐代次校验**文件/封存字节/有效前缀/每行 JSON/CRC**，
        #     但事件配对、唯一性与顺序必须跨代：先把按 gen 顺序的完整逻辑事件流
        #     拼接起来，最后统一 _validate_events（与 read_events 同一语义）。
        truncated = False
        truncated_op_id: str | None = None
        truncated_bytes = 0
        logical_events: list[dict[str, Any]] = []
        for index, generation in enumerate(generations):
            path = os.path.join(self.dir, generation.file)
            try:
                data = _read_bytes(path)
            except JournalCorrupted as exc:
                return JournalState(JOURNAL_CORRUPTED, str(exc))
            is_last = index == len(generations) - 1
            if generation.status == "sealed":
                usable = generation.usable_bytes
                if usable is None:
                    return JournalState(
                        JOURNAL_CORRUPTED, f"代次 {generation.gen} 已封存但缺少 usable_bytes"
                    )
                if generation.file_bytes is not None and len(data) != generation.file_bytes:
                    return JournalState(
                        JOURNAL_CORRUPTED,
                        f"代次 {generation.gen} 长度与封存记录不符（旧证据被改动）",
                    )
                if generation.crc32_full is not None and crc32_hex(data) != generation.crc32_full:
                    return JournalState(
                        JOURNAL_CORRUPTED, f"代次 {generation.gen} 的 crc32_full 与磁盘不符"
                    )
                if usable > len(data):
                    return JournalState(
                        JOURNAL_CORRUPTED, f"代次 {generation.gen} 的 usable_bytes 超过文件长度"
                    )
                lines, tail = self._split_complete_lines(data[:usable])
                if tail:
                    return JournalState(
                        JOURNAL_CORRUPTED, f"代次 {generation.gen} 的有效前缀内存在不完整行"
                    )
            else:
                lines, tail = self._split_complete_lines(data)
                if tail:
                    if not is_last:
                        return JournalState(
                            JOURNAL_CORRUPTED,
                            f"代次 {generation.gen} 存在不完整尾行但并非最后一代",
                        )
                    truncated = True
                    truncated_op_id = _peek_op_id(tail)
                    truncated_bytes = len(tail)
            try:
                logical_events.extend(
                    self._parse_line(ln, where=f"{generation.file}:{i + 1}")
                    for i, ln in enumerate(lines)
                )
            except JournalCorrupted as exc:
                return JournalState(JOURNAL_CORRUPTED, str(exc))

        # 3a-2) 在**整条逻辑事件流**上统一校验配对/唯一性/顺序
        try:
            self._validate_events(logical_events, where=f"{self.prefix}-{self.plan_id}")
        except JournalCorrupted as exc:
            return JournalState(JOURNAL_CORRUPTED, str(exc))

        # 3b) 未登记文件
        unregistered = [(n, name) for n, name in existing if name not in registered]
        if len(unregistered) > 1:
            return JournalState(JOURNAL_CORRUPTED, "存在多个未登记代次文件")
        pending_gen = pending_file = None
        if unregistered:
            gen_number, name = unregistered[0]
            expected = (max(numbers) if numbers else 0) + 1
            if gen_number != expected:
                return JournalState(
                    JOURNAL_CORRUPTED,
                    f"未登记代次文件序号不符（得到 {gen_number}，期望 {expected}）：{name}",
                )
            path = os.path.join(self.dir, name)
            try:
                st = os.lstat(path)
            except OSError as exc:
                return JournalState(JOURNAL_CORRUPTED, f"无法检查未登记代次文件：{exc}")
            if statmod.S_ISLNK(st.st_mode):
                return JournalState(JOURNAL_CORRUPTED, f"未登记代次文件是符号链接：{name}")
            if not statmod.S_ISREG(st.st_mode):
                return JournalState(JOURNAL_CORRUPTED, f"未登记代次文件不是普通文件：{name}")
            if st.st_size != 0:
                return JournalState(JOURNAL_CORRUPTED, f"未登记代次文件非空：{name}")
            pending_gen, pending_file = gen_number, name

        if pending_gen is not None:
            combined = truncated and pending_gen == (active_gen + 1 if active_gen else -1)
            return JournalState(
                JOURNAL_REPAIRABLE,
                (
                    f"存在未登记的空代次文件 {pending_file}（清单发布被中断）"
                    + ("且活动代次有合法截断尾行" if combined else "")
                    + "，可完成发布"
                    if combined
                    else f"存在未登记的空代次文件 {pending_file}（清单发布被中断），可完成发布"
                ),
                repair_kind=(
                    "complete_pending_publish" if combined else "register_pending_generation"
                ),
                truncated=truncated,
                truncated_op_id=truncated_op_id,
                truncated_bytes=truncated_bytes,
                pending_gen=pending_gen,
                pending_file=pending_file,
                generations=len(generations),
            )
        if truncated:
            return JournalState(
                JOURNAL_REPAIRABLE,
                "活动代次存在合法截断尾行，可封存并开启新代次",
                repair_kind="seal_truncated_tail",
                truncated=True,
                truncated_op_id=truncated_op_id,
                truncated_bytes=truncated_bytes,
                generations=len(generations),
            )
        return JournalState(JOURNAL_HEALTHY, "日志健康", generations=len(generations))

    def _state_readonly(
        self,
        *,
        report: ReadReport | None = None,
        allow_repair: bool = False,
        allow_manifest_rebuild: bool | None = None,
        tolerate_pending: bool = False,
    ) -> tuple[list[Generation], dict[str, Any], tuple[int, str] | None]:
        """只读解析（不写任何文件）。

        ``allow_repair=True`` 时，对**分类为 repairable** 的状态执行明确修复
        （重建单代次清单 / 登记空 N+1 代 / 封存截断尾行）；``allow_repair=False``
        时，repairable 与 corrupted 都以异常拒绝——用
        :class:`JournalNeedsRepair`（可修复）或 :class:`JournalCorrupted`（损坏）
        区分，由调用方决定是否提示 ``recover``。
        """
        report = report if report is not None else ReadReport()
        state = self.classify()
        if state.corrupted:
            raise JournalCorrupted(state.detail)
        if state.healthy:
            return self._load_healthy_state(report=report)
        # repairable
        if not allow_repair:
            raise JournalNeedsRepair(
                f"{state.detail}；尚未修复，请运行 recover（执行/撤销入口拒绝继续）", state
            )
        return self._apply_repair(state, report=report)

    def _load_healthy_state(
        self, *, report: ReadReport
    ) -> tuple[list[Generation], dict[str, Any], tuple[int, str] | None]:
        manifest = self._load_manifest()
        generations = self._generations_from_manifest(manifest) if manifest else []
        if not generations:
            return [], {
                "schema_version": 1,
                "plan_id": self.plan_id,
                "log_kind": self.log_kind,
                "active_gen": None,
                "generations": [],
            }, None
        return generations, manifest or {}, None

    def _apply_repair(
        self, state: JournalState, *, report: ReadReport
    ) -> tuple[list[Generation], dict[str, Any], tuple[int, str] | None]:
        """执行一次**明确**的修复动作（只在 recover / 封存阶段调用）。"""
        manifest = self._load_manifest()
        existing = self._existing_generations()

        if state.repair_kind == "rebuild_manifest_single_generation":
            gen_number, name = existing[0]
            generation = Generation(gen=gen_number, file=name, status="active", opened_at=_now())
            manifest = self._manifest_dict([generation], gen_number)
            self._write_manifest(manifest)
            report.manifest_rebuilt = True
            report.notes.append(f"清单丢失：已按唯一代次文件重建单代次清单（{name}）")
            if state.truncated:
                report.truncated_tail_line = None
                report.truncated_op_id = state.truncated_op_id
                report.truncated_bytes = state.truncated_bytes
            return [generation], manifest, None

        generations = self._generations_from_manifest(manifest)

        if state.repair_kind == "register_pending_generation":
            gen_number = int(state.pending_gen)
            name = str(state.pending_file)
            generations = generations + [
                Generation(gen=gen_number, file=name, status="active", opened_at=_now())
            ]
            manifest = self._manifest_dict(generations, gen_number)
            self._write_manifest(manifest)
            report.repaired_publish = True
            report.notes.append(f"完成被中断的清单发布：登记 {name} 为活动代次（不覆盖文件）")
            return generations, manifest, None

        if state.repair_kind == "complete_pending_publish":
            # 复合修复：先封存带截断尾行的活动代次，再复用未登记的空 N+1 代
            active = self._active_generation(generations)
            if active is None:
                raise JournalCorrupted("没有活动代次，无法完成发布")
            gen_number = int(state.pending_gen)
            name = str(state.pending_file)
            data = _read_bytes(os.path.join(self.dir, active.file))
            _lines, tail = self._split_complete_lines(data)
            if not tail:
                return self._load_healthy_state(report=report)
            usable = len(data) - len(tail)
            active.status = "sealed"
            active.sealed_reason = "truncated_tail_line"
            active.usable_bytes = usable
            active.file_bytes = len(data)
            active.truncated_bytes = len(tail)
            active.crc32_full = crc32_hex(data)
            active.sealed_at = _now()
            new_generation = Generation(
                gen=gen_number, file=name, status="active", opened_at=_now()
            )
            manifest = self._manifest_dict(generations + [new_generation], gen_number)
            try:
                self._write_manifest(manifest)
            except OSError as exc:
                raise JournalWriteFailed(f"清单发布失败：{exc}") from exc
            report.sealed_now.append(active.gen)
            report.repaired_publish = True
            report.truncated_tail_line = tail.decode("utf-8", "replace")
            report.truncated_op_id = _peek_op_id(tail)
            report.truncated_bytes = len(tail)
            report.notes.append(
                f"封存第 {active.gen} 代（保留 {len(data)} 字节，逻辑丢弃尾部 {len(tail)} 字节）"
                f"并完成被中断的清单发布：{name} 成为活动代次（不覆盖文件）"
            )
            return generations + [new_generation], manifest, None

        if state.repair_kind == "seal_truncated_tail":
            active = self._active_generation(generations)
            if active is None:
                raise JournalCorrupted("没有活动代次，无法封存截断尾行")
            data = _read_bytes(os.path.join(self.dir, active.file))
            _lines, tail = self._split_complete_lines(data)
            if not tail:
                # 已被其他流程修复（幂等）
                return self._load_healthy_state(report=report)
            usable = len(data) - len(tail)
            active.status = "sealed"
            active.sealed_reason = "truncated_tail_line"
            active.usable_bytes = usable
            active.file_bytes = len(data)
            active.truncated_bytes = len(tail)
            active.crc32_full = crc32_hex(data)
            active.sealed_at = _now()
            next_gen = active.gen + 1
            self._create_empty_generation(next_gen)
            new_generation = Generation(
                gen=next_gen, file=self.generation_name(next_gen), status="active", opened_at=_now()
            )
            manifest = self._manifest_dict(generations + [new_generation], next_gen)
            try:
                self._write_manifest(manifest)
            except OSError as exc:
                raise JournalWriteFailed(f"清单发布失败：{exc}") from exc
            report.sealed_now.append(active.gen)
            report.truncated_tail_line = tail.decode("utf-8", "replace")
            report.truncated_op_id = _peek_op_id(tail)
            report.truncated_bytes = len(tail)
            report.notes.append(
                f"末行截断：封存第 {active.gen} 代（保留 {len(data)} 字节，逻辑丢弃尾部 "
                f"{len(tail)} 字节），新记录写入第 {next_gen} 代"
            )
            return generations + [new_generation], manifest, None

        raise JournalCorrupted(f"未知的修复类型：{state.repair_kind!r}")

    #: 允许的组合修复步数上限（重建清单 + 完成发布 + 封存截断）
    MAX_REPAIR_STEPS = 4

    def repair_if_needed(self, *, report: ReadReport | None = None) -> JournalState:
        """把日志修复到 **healthy**：有界、可验证、幂等。

        - healthy → 不动；
        - repairable → 按分类结果执行**明确修复**，每次修复后重新分类；
          合法组合（例如「单代次清单缺失 + 截断尾行」→ 重建清单，再封存截断）
          会在同一次公开 recover 内完成；
        - 每一步都必须**产生可验证进展**（``repair_kind``/``truncated``/代数发生变化），
          否则抛错，**绝不盲目循环、绝不吞异常**；
        - corrupted 或超出步数上限仍非 healthy → 抛 :class:`JournalCorrupted`；
        - 任何一步都**不改写旧代次字节**（封存只写清单与新建空代次）。
        """
        report = report if report is not None else ReadReport()
        state = self.classify()
        for _step in range(self.MAX_REPAIR_STEPS):
            if state.corrupted:
                raise JournalCorrupted(state.detail)
            if state.healthy:
                return state
            previous = state
            self._apply_repair(state, report=report)
            state = self.classify()
            if state.corrupted:
                raise JournalCorrupted(state.detail)
            if not state.healthy:
                progressed = (
                    state.repair_kind != previous.repair_kind
                    or state.truncated != previous.truncated
                    or state.generations != previous.generations
                    or state.pending_gen != previous.pending_gen
                )
                if not progressed:
                    raise JournalCorrupted(
                        f"修复未取得可验证进展（仍为 {state.repair_kind or state.classification}）："
                        f"{state.detail}"
                    )
        raise JournalCorrupted(
            f"修复在 {self.MAX_REPAIR_STEPS} 步内未达到 healthy（当前 {state.classification}："
            f"{state.detail}）"
        )

    def _state(
        self, *, report: ReadReport | None = None, allow_repair: bool = True
    ) -> tuple[list[Generation], dict[str, Any], tuple[int, str] | None]:
        """兼容入口：默认允许修复（历史行为）。"""
        return self._state_readonly(report=report, allow_repair=allow_repair)

    def probe_integrity(self) -> dict[str, Any]:
        """**无副作用**完整性探测：返回字典（含结构化 ``state``）。

        分类只依据内容验证（见 :meth:`classify`），**不依据异常文本关键词**；
        本方法不创建目录、不写清单、不改动任何字节。

        返回**纯 JSON 可序列化**的字典（向后兼容 ``ok``/``kind``/``needs_repair``）：
        ``classification``、``detail``、``repair_kind``、``truncated``、
        ``truncated_op_id``、``truncated_bytes``、``pending_gen``、
        ``pending_file``、``generations``。需要结构化对象请用 :meth:`classify`。
        """
        state = self.classify()
        return {
            "ok": state.healthy or state.repairable,
            "kind": state.classification,
            "classification": state.classification,
            "needs_repair": state.repairable,
            "detail": state.detail,
            "repair_kind": state.repair_kind,
            "truncated": state.truncated,
            "truncated_op_id": state.truncated_op_id,
            "truncated_bytes": state.truncated_bytes,
            "pending_gen": state.pending_gen,
            "pending_file": state.pending_file,
            "generations": state.generations,
        }

    def read_events(
        self, *, report: ReadReport | None = None, allow_repair: bool = False
    ) -> tuple[list[dict[str, Any]], ReadReport]:
        """读取全部合法事件（按代次顺序）。

        默认 ``allow_repair=False``：**只读**，不创建目录、不写清单、不封存、不修复；
        healthy 直接返回，repairable 抛 :class:`JournalNeedsRepair`，
        corrupted 抛 :class:`JournalCorrupted`。
        """
        report = report if report is not None else ReadReport()
        generations, _, _ = self._state_readonly(report=report, allow_repair=allow_repair)
        if not generations:
            return [], report

        events: list[dict[str, Any]] = []
        for index, generation in enumerate(generations):
            data = _read_bytes(os.path.join(self.dir, generation.file))
            is_last = index == len(generations) - 1
            if generation.status == "sealed":
                usable = generation.usable_bytes
                if usable is None:
                    raise JournalCorrupted(f"代次 {generation.gen} 已封存但缺少 usable_bytes")
                if generation.file_bytes is not None and len(data) != generation.file_bytes:
                    raise JournalCorrupted(
                        f"代次 {generation.gen} 的长度与封存记录不符（旧证据被改动？）"
                    )
                if generation.crc32_full is not None and crc32_hex(data) != generation.crc32_full:
                    raise JournalCorrupted(
                        f"代次 {generation.gen} 的 crc32_full 与磁盘不符（旧证据被改动？）"
                    )
                if usable > len(data):
                    raise JournalCorrupted(f"代次 {generation.gen} 的 usable_bytes 超过文件长度")
                lines, tail = self._split_complete_lines(data[:usable])
                if tail:
                    raise JournalCorrupted(f"代次 {generation.gen} 的有效前缀内存在不完整行")
            else:
                lines, tail = self._split_complete_lines(data)
                if tail:
                    if not is_last:
                        raise JournalCorrupted(
                            f"代次 {generation.gen} 存在不完整尾行但并非最后一代"
                        )
                    report.truncated_tail_line = tail.decode("utf-8", "replace")
                    report.truncated_op_id = _peek_op_id(tail)
                    report.truncated_bytes = len(tail)
            events.extend(
                self._parse_line(ln, where=f"{generation.file}:{i + 1}") for i, ln in enumerate(lines)
            )

        self._validate_events(events, where=f"{self.prefix}-{self.plan_id}")
        return events, report

    def fold(
        self, *, report: ReadReport | None = None, allow_repair: bool = False
    ) -> tuple[dict[str, dict[str, Any]], ReadReport]:
        events, report = self.read_events(report=report, allow_repair=allow_repair)
        fold: dict[str, dict[str, Any]] = {}
        for payload in events:
            op_id = str(payload["op_id"])
            slot = fold.setdefault(
                op_id,
                {
                    "op_id": op_id,
                    "intent": None,
                    "result": None,
                    "undo_intent": None,
                    "undo_result": None,
                    "undo_skip": None,
                },
            )
            event = payload["event"]
            slot[event] = payload
        return fold, report

    def _active_generation(self, generations: list[Generation]) -> Generation | None:
        for generation in reversed(generations):
            if generation.status == "active":
                return generation
        return None

    # --- 写入 ---------------------------------------------------------------
    def ensure_sealed(self, *, report: ReadReport | None = None) -> ReadReport:
        """取得锁后、写任何新记录之前调用（修复入口，幂等）。

        - healthy → 不做任何事；
        - repairable（单代次清单缺失 / 空 N+1 发布中断 / 合法截断尾行）→ 执行
          **明确且经过内容校验**的修复，旧字节永不改动；
        - corrupted → 抛 :class:`JournalCorrupted`，不改动任何文件。
        """
        report = report if report is not None else ReadReport()
        self.repair_if_needed(report=report)
        return report

    def _create_empty_generation(self, gen: int) -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        path = self.generation_file(gen)
        if os.path.lexists(path):
            st = os.lstat(path)
            if not statmod.S_ISREG(st.st_mode) or st.st_size != 0:
                raise JournalCorrupted(f"待新建的代次文件已存在且不可复用：{path}")
            return
        try:
            fd = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
        except OSError as exc:
            raise JournalWriteFailed(f"无法排他新建代次文件：{path}（{exc}）") from exc
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def append(self, payload: Mapping[str, Any], *, report: ReadReport | None = None) -> None:
        """追加一条事件（自动补 ts/plan_id/crc32），逐行 flush + fsync。"""
        report = report if report is not None else ReadReport()
        event = payload.get("event")
        if event not in self.allowed_events:
            raise JournalCorrupted(
                f"事件 {event!r} 不属于 {self.log_kind} 日志的合法事件集合 "
                f"{sorted(self.allowed_events)}；拒绝写入"
            )
        self.ensure_sealed(report=report)
        prior, _ = self.fold(report=report)
        slot = prior.get(str(payload.get("op_id")) or "", {})
        if event == "result" and slot.get("intent") is None:
            raise JournalCorrupted("拒绝写入无在先 intent 的孤立 result")
        if event == "undo_result" and slot.get("undo_intent") is None:
            raise JournalCorrupted("拒绝写入无在先 undo_intent 的孤立 undo_result")
        if event == "undo_skip" and (
            slot.get("undo_intent") is not None or slot.get("undo_result") is not None
        ):
            raise JournalCorrupted("拒绝写入与 undo_intent/undo_result 并存的 undo_skip")
        generations, _, pending = self._state(report=report)
        active = self._active_generation(generations)
        if active is None:
            # 首次写入：建立第 1 代
            self._create_empty_generation(1)
            generations = generations + [
                Generation(gen=1, file=self.generation_name(1), status="active", opened_at=_now())
            ]
            self._write_manifest(self._manifest_dict(generations, 1))
            active = generations[-1]

        record = dict(payload)
        record.setdefault("ts", _now())
        record.setdefault("plan_id", self.plan_id)
        record["crc32"] = line_crc32(record)
        line = (
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

        path = os.path.join(self.dir, active.file)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise JournalWriteFailed(f"无法打开日志追加：{path}（{exc}）") from exc
        try:
            os.write(fd, line)
            os.fsync(fd)
        except OSError as exc:
            raise JournalWriteFailed(f"日志写入/fsync 失败：{path}（{exc}）") from exc
        finally:
            os.close(fd)


def make_event(event: str, op_id: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"event": event, "op_id": op_id}
    payload.update(fields)
    return payload
