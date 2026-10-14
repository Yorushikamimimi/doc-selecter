"""``plan_hash`` 规范化编码契约（docs/MVP方案.md §5.1.1）。

算法::

    plan_hash = "sha256:" + sha256(canonical_json(plan_body)).hexdigest()

规范化要求（执行、撤销、GUI、CLI 共用本模块，禁止各自实现）:

1. UTF-8 JSON object，``sort_keys=True``、``separators=(",", ":")``、``allow_nan=False``；
2. 字符串做 NFC 归一化；
3. 路径为规范化绝对路径（abspath → realpath → normpath，分隔符统一为 ``/``）；
4. 整数十进制、无浮点；
5. ``operations`` 按 ``op_id`` 升序；``category_selection`` 按固定类别顺序；
6. ``plan_body`` 不含任何时间戳。

**覆盖字段**：schema_version、rules_version、downloads_dir、target_root、
category_selection、**selected_scope**（显式选择根）、**scan_scope**（实际授权扫描范围）、每个 operation 的
op_id/source/dest/category/classification_basis/**rel_path**/
identity(st_dev,st_ino,size_bytes,mtime_ns,mode_type)。

**排除字段**：``plan_hash`` 自身、``plan_id``、``created_at``/``confirmed_at``、
确认口令、``counts``/``excluded``、``state_dir``、任何运行期字段。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import unicodedata
from typing import Any, Iterable, Mapping

PLAN_HASH_ALG = "sha256/canonical-json-v1"
PLAN_SCHEMA_VERSION = 3
V3_HASH_ALG = "sha256/canonical-json-v2"

#: 参与哈希的 operation 字段（``rel_path`` 自 schema v2 起绑定扫描范围与相对层级）
OPERATION_HASH_FIELDS = (
    "op_id", "source", "dest", "category", "classification_basis", "rel_path",
)
#: 参与哈希的身份字段（五项，全部必填）
IDENTITY_FIELDS = ("st_dev", "st_ino", "size_bytes", "mtime_ns", "mode_type")


class PlanHashError(ValueError):
    """计划结构不符合哈希契约。"""


# --- 规范化原语 ---------------------------------------------------------------
def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def normalize_path(path: str) -> str:
    """规范化绝对路径：abspath → normpath → realpath → 分隔符统一 ``/``。

    ``realpath`` 会解析符号链接，因此**调用方必须先**用
    :func:`reject_symlink_segments_between` 对**原始路径**做链接检查；
    本函数只负责契约要求的规范化编码，不做安全判定。
    """
    if not path:
        raise PlanHashError("路径不能为空")
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        raise PlanHashError(f"路径必须是绝对路径：{path}")
    resolved = os.path.normpath(os.path.abspath(expanded))
    resolved = os.path.normpath(os.path.realpath(resolved))
    return normalize_text(resolved.replace(os.sep, "/"))


def path_is_within(base: str, path: str) -> bool:
    """``path`` 是否等于 ``base`` 或位于其下（按 ``normpath`` 判断，不解析链接）。"""
    base_n = os.path.normpath(base)
    path_n = os.path.normpath(path)
    return path_n == base_n or path_n.startswith(base_n + os.sep)


def reject_symlink_segments_between(
    anchor: str, path: str, *, skip_last: bool = False
) -> None:
    """在**解析链接之前**检查：``anchor`` 到 ``path`` 之间的每一段都不得是符号链接。

    语义（P1 修正后的关键）：

    - ``anchor`` 是**信任锚点**（下载目录），其自身的系统级别名
      （macOS ``/var`` → ``/private/var``）不属于「相对锚点的链接段」，因此
      锚点**单独**做一次 ``realpath`` 归一；
    - 待检查的 ``path`` **保留原始相对段**：先用 ``normpath``（不解析链接）
      求出它相对原始锚点的段序列，再以「已归一的锚点」为起点**逐段**拼回并
      ``islink`` 检查。**绝不**先把整条 path 送进 ``realpath``——那会把内部的
      链接（例如「文档资料」指向「音视频」）提前抹掉，从而漏检；
    - 每段检查时同时做一次 ``realpath`` 边界确认：若某段是链接且解析后逃出
      锚点，则立刻判越界。

    这样 macOS 的系统别名不会误伤，而锚点内部的类别链接一定被拒绝。

    ``skip_last=True`` 时只检查父目录链（文件自身是否为链接由 ``lstat`` 判定）。
    """
    if not path:
        raise PlanHashError("路径不能为空")
    raw_anchor = os.path.normpath(os.path.expanduser(anchor))
    raw_path = os.path.normpath(os.path.expanduser(path))
    if not os.path.isabs(raw_path):
        raise PlanHashError(f"路径必须是绝对路径：{path}")
    if not path_is_within(raw_anchor, raw_path):
        raise PlanHashError(f"路径越界（不在 {raw_anchor} 之下）：{raw_path}")

    # 锚点自身的系统别名单独归一（不解析待检查子路径），
    # 然后把**原始相对段**重挂到归一后的锚点上逐段检查。
    anchor_n = os.path.normpath(os.path.realpath(raw_anchor))
    relative = os.path.relpath(raw_path, raw_anchor)
    if relative in (".", ""):
        return
    segments = relative.split(os.sep)
    if skip_last:
        segments = segments[:-1]

    current = anchor_n
    for part in segments:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise PlanHashError(f"拒绝：路径段是符号链接（{current}）")
        # 链接之外：该段解析后仍必须在锚点内（防止悬空/越界段）
        resolved_here = os.path.normpath(os.path.realpath(current))
        if not path_is_within(anchor_n, resolved_here):
            raise PlanHashError(f"路径越界（段 {current} 解析到锚点之外）：{resolved_here}")


def canonical_json(obj: Any) -> str:
    """把对象编码为规范化 JSON 字符串（不写盘）。"""
    normalized = _normalize_value(obj)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if value is None:
        return None
    if isinstance(value, float):
        raise PlanHashError("哈希契约禁止浮点数")
    if isinstance(value, Mapping):
        return {normalize_text(str(k)): _normalize_value(v) for k, v in sorted(value.items(),key=lambda item:str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(v) for v in value]
    raise PlanHashError(f"哈希契约不支持的类型：{type(value).__name__}")


def _require_int(container: Mapping[str, Any], key: str, where: str) -> int:
    if key not in container:
        raise PlanHashError(f"{where} 缺少字段 {key}")
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanHashError(f"{where}.{key} 必须是整数，实际为 {type(value).__name__}")
    return value


def project_identity(identity: Mapping[str, Any], where: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in IDENTITY_FIELDS:
        if field == "mode_type":
            if field not in identity:
                raise PlanHashError(f"{where} 缺少字段 mode_type")
            out[field] = normalize_text(str(identity[field]))
        else:
            out[field] = _require_int(identity, field, where)
    return out


def _stored_path(path: str) -> str:
    """展示校验只用已存规范路径，不能为展示历史记录而 stat 私密文件。

    不解析当前文件系统；执行/撤销/恢复仍使用默认 realpath 与 binding。
    非规范存储路径直接拒绝，不能把路径规范化差异隐藏掉。
    """
    normalized = os.path.normpath(path).replace(os.sep, "/")
    if not os.path.isabs(path) or path.replace(os.sep, "/") != normalized:
        raise PlanHashError("计划存储路径不是规范绝对路径")
    return normalize_text(normalized)


def project_operation(operation: Mapping[str, Any], index: int, *,
                      resolve_paths: bool = True) -> dict[str, Any]:
    where = f"operations[{index}]"
    out: dict[str, Any] = {}
    for field in OPERATION_HASH_FIELDS:
        if field not in operation:
            raise PlanHashError(f"{where} 缺少字段 {field}")
        value = operation[field]
        if not isinstance(value, str):
            raise PlanHashError(f"{where}.{field} 必须是字符串")
        if field in ("source", "dest"):
            out[field] = normalize_path(value) if resolve_paths else _stored_path(value)
        elif field == "rel_path":
            rel = value.strip().strip("/")
            if os.path.isabs(rel) or rel == ".." or rel.startswith("../") or "/../" in rel:
                raise PlanHashError(f"{where}.rel_path 含越界路径：{value!r}")
            out[field] = normalize_text(rel.replace(os.sep, "/"))
        else:
            out[field] = normalize_text(value)
    out["identity"] = project_identity(operation.get("identity", {}), f"{where}.identity")
    if "document_subcategory" in operation:
        value = operation["document_subcategory"]
        if value is not None and not isinstance(value, str):
            raise PlanHashError(f"{where}.document_subcategory 必须是字符串或 null")
        out["document_subcategory"] = None if value is None else normalize_text(value)
    return out


def project_plan_body(plan: Mapping[str, Any], *, resolve_paths: bool = True) -> dict[str, Any]:
    """按契约只取覆盖字段，构造参与哈希的 ``plan_body``（绝不包含 hash 自身）。"""
    for key in (
        "schema_version", "rules_version", "downloads_dir", "target_root",
        "category_selection", "operations", "scan_scope",
    ):
        if key not in plan:
            raise PlanHashError(f"计划缺少字段 {key}")

    scope = plan["scan_scope"]
    if not isinstance(scope, (list, tuple)) or not scope:
        raise PlanHashError("scan_scope 必须是非空列表")
    ordered_scope: list[str] = []
    for item in scope:
        if not isinstance(item, str):
            raise PlanHashError("scan_scope 的每一项必须是字符串")
        rel = item.strip().strip("/")
        if rel and (os.path.isabs(rel) or rel == ".." or rel.startswith("../")):
            raise PlanHashError(f"scan_scope 含越界相对路径：{item!r}")
        norm = normalize_text(rel.replace(os.sep, "/"))
        if norm and norm not in ordered_scope:
            ordered_scope.append(norm)
    ordered_scope.sort()

    selection = plan["category_selection"]
    if not isinstance(selection, (list, tuple)):
        raise PlanHashError("category_selection 必须是列表")
    # 固定类别顺序（不接受输入顺序）
    from .rules import CATEGORIES

    unknown = [c for c in selection if c not in CATEGORIES]
    if unknown:
        raise PlanHashError(f"category_selection 含未知类别：{unknown}")
    ordered_selection = [c for c in CATEGORIES if c in set(selection)]

    operations = plan["operations"]
    if not isinstance(operations, (list, tuple)):
        raise PlanHashError("operations 必须是列表")
    ops = [project_operation(op, i, resolve_paths=resolve_paths) for i, op in enumerate(operations)]
    ops.sort(key=lambda op: op["op_id"])
    op_ids = [op["op_id"] for op in ops]
    if len(set(op_ids)) != len(op_ids):
        raise PlanHashError("operations 存在重复的 op_id")

    body = {
        "schema_version": _require_int(plan, "schema_version", "plan"),
        "rules_version": normalize_text(str(plan["rules_version"])),
        "downloads_dir": (normalize_path if resolve_paths else _stored_path)(str(plan["downloads_dir"])),
        "target_root": (normalize_path if resolve_paths else _stored_path)(str(plan["target_root"])),
        "category_selection": ordered_selection,
        "scan_scope": ordered_scope,
        "operations": ops,
    }
    if "selected_scope" in plan:
        from .scanner import normalize_scope
        body["selected_scope"] = normalize_scope(plan["selected_scope"])
    return body


def compute_plan_hash(plan: Mapping[str, Any], *, resolve_paths: bool = True) -> str:
    """计算 ``sha256:<hex>``。同一 plan_body 必然得到同一结果。"""
    body = project_plan_body(plan, resolve_paths=resolve_paths if plan.get("schema_version") not in (3, 4) else False)
    if plan.get("schema_version") in (3, 4):
        for key in ("recursive", "archive", "directory_operations", "dir_identities"):
            if key not in plan:
                raise PlanHashError(f"v3 缺少字段 {key}")
            body[key] = plan[key]
        body["plan_hash_alg"] = plan.get("plan_hash_alg")
        for projected,op in zip(body["operations"],sorted(plan["operations"],key=lambda v:v["op_id"])):
            projected["kind"]=op.get("kind")
    if plan.get("schema_version") == 4:
        for key in ("plan_type", "plan_id", "remaining_candidates", "remaining_selection"):
            if key not in plan:
                raise PlanHashError(f"v4 缺少字段 {key}")
            body[key] = plan[key]
    digest = hashlib.sha256(canonical_bytes(body)).hexdigest()
    return f"sha256:{digest}"


def verify_plan_hash(plan: Mapping[str, Any], *, resolve_paths: bool = True) -> bool:
    stored = plan.get("plan_hash")
    if not isinstance(stored, str) or not stored:
        return False
    return stored == compute_plan_hash(plan, resolve_paths=resolve_paths)


def plan_body_digest(plan: Mapping[str, Any]) -> str:
    """仅十六进制摘要（便于日志/展示）。"""
    return compute_plan_hash(plan).split(":", 1)[1]


# --- 计划标识 -----------------------------------------------------------------
def new_plan_id(now: float | None = None) -> str:
    """生成形如 ``plan-20261001-223045-a1b2c3`` 的计划 id。

    只使用 ``[0-9a-z-]``，避免文件名在不同文件系统上的转义问题。
    """
    ts = time.localtime(now if now is not None else time.time())
    stamp = time.strftime("%Y%m%d-%H%M%S", ts)
    return f"plan-{stamp}-{secrets.token_hex(3)}"


def assert_plan_consistent(plan: Mapping[str, Any]) -> None:
    """生成前的自检：结构合法且哈希可复现。"""
    expected = compute_plan_hash(plan)
    stored = plan.get("plan_hash")
    if stored != expected:
        raise PlanHashError(f"plan_hash 与 plan_body 不一致：stored={stored!r} expected={expected!r}")


def sorted_selection(categories: Iterable[str]) -> list[str]:
    from .rules import CATEGORIES

    chosen = set(categories)
    return [c for c in CATEGORIES if c in chosen]
