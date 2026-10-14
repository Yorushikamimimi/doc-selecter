"""预览计划生成与读写。

计划文件一经生成即**不可变**：写入用排他新建（``O_EXCL``），绝不覆盖同名文件。
"""

from __future__ import annotations

import json
import os
import stat
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

from . import RULES_VERSION
from .config import Settings
from .plan_hash import (
    PLAN_HASH_ALG,
    PLAN_SCHEMA_VERSION,
    compute_plan_hash,
    new_plan_id,
    normalize_path,
    reject_symlink_segments_between,
    sorted_selection,
    verify_plan_hash,
)
from .rules import (CATEGORIES, EXCLUDED_SENSITIVE, EXCLUSION_LABELS,
                    document_subcategory, is_sensitive_relative_path,
                    redact_private_text)
from .scanner import ScanResult, scan_scope


class PlanError(RuntimeError):
    """计划读写错误。"""


class MoveAdmissionRefused(PlanError):
    """只拒绝新分类移动；不改变历史记录的格式/哈希/还原校验。"""


@dataclass
class PlanBuild:
    plan: dict[str, Any]
    scan: ScanResult


def _excluded_payload(scan: ScanResult) -> dict[str, Any]:
    grouped: dict[str, list[str]] = {}
    reasons: dict[str, list[str]] = {}
    for entry in scan.excluded:
        label = EXCLUSION_LABELS.get(entry.code, entry.code)
        grouped.setdefault(label, []).append(entry.rel_path)
        reasons.setdefault(label, [])
        if entry.basis and entry.basis not in reasons[label]:
            reasons[label].append(entry.basis)
    payload: dict[str, Any] = {k: sorted(v) for k, v in sorted(grouped.items())}
    # 疑似敏感项与疑似敏感目录：只保留数量，绝不保存名称
    payload["疑似敏感项"] = {"count": scan.sensitive_count, "names": None}
    payload["跳过原因"] = {k: sorted(v) for k, v in sorted(reasons.items())}
    payload["元数据读取失败"] = {
        "count": sum(scan.metadata_failure_counts.values()),
        "types": dict(scan.metadata_failure_counts),
    }
    if scan.scan_issues:
        payload["递归读取失败/扫描变化"] = {
            "count": len(scan.scan_issues),
            "items": [
                {"path": issue.rel_path, "code": issue.code, "detail": issue.detail}
                for issue in scan.scan_issues
            ],
        }
    return payload


def build_plan(
    settings: Settings,
    *,
    category_selection: list[str] | None = None,
    scope_dirs=None,
    recursive: bool = False,
    scan: ScanResult | None = None,
    plan_id: str | None = None,
    now: float | None = None,
    archive_emptied_dirs: bool = False,
) -> PlanBuild:
    """只读扫描并构造计划（不写盘）。

    ``recursive=True`` 时只对 ``scope_dirs``（用户选中的目录）递归；默认仅顶层。
    目标路径为 ``目标根/<类别>/<原相对目录链>/<原文件名>``，**保留原层级**。
    """
    selection = sorted_selection(CATEGORIES if category_selection is None else category_selection)
    # 目标根：解析链接之前先检查（链接根会把写入重定向到下载目录之外）
    reject_symlink_segments_between(
        settings.downloads_dir,
        os.path.join(settings.downloads_dir, settings.target_parent_name),
    )
    if os.path.islink(settings.target_root):
        raise PlanError(f"目标根是符号链接，拒绝生成计划：{settings.target_root}")
    check_target_root_containment(settings.downloads_dir, settings.target_parent_name)
    if scan is None:
        scan = scan_scope(
            settings.downloads_dir,
            target_parent_name=settings.target_parent_name,
            scope_dirs=scope_dirs,
            recursive=recursive,
        )

    visible_paths = (list(scan.selected_scope) + list(scan.scan_scope)
                     + [e.rel_path for e in scan.included + scan.excluded]
                     + [p for p, _ in scan.metadata_failures]
                     + [issue.rel_path for issue in scan.scan_issues]
                     + list(scan.added_during_scan) + list(scan.removed_during_scan))
    if any(is_sensitive_relative_path(p) for p in visible_paths):
        raise PlanError("扫描结果不符合当前筛查规则，请重新生成预览。")

    if archive_emptied_dirs and scan.failure_count:
        raise PlanError("目录候选扫描不完整，请重新预览")
    chosen = set(selection)
    operations: list[dict[str, Any]] = []
    index = 0
    for entry in sorted(scan.included, key=lambda e: e.rel_path):
        category = entry.classification.category or CATEGORIES[-1]
        if category not in chosen:
            continue
        assert entry.identity is not None
        index += 1
        subcategory = (
            document_subcategory(entry.classification.extension)
            if category == "文档资料" and not entry.rel_dir else None
        )
        operation = {
            "op_id": f"op-{index:04d}",
            "kind": "file",
            "rel_path": entry.rel_path,
            "source": unicodedata.normalize("NFC", os.path.join(settings.canonical_downloads_dir, *entry.rel_path.split("/"))),
            "dest": unicodedata.normalize("NFC", settings.dest_for(category, entry.name, entry.rel_dir, subcategory)),
            "category": category,
            "classification_basis": entry.classification.basis,
            "identity": entry.identity.as_dict(),
        }
        if subcategory is not None:
            operation["document_subcategory"] = subcategory
        operations.append(
            operation
        )

    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_id": plan_id or new_plan_id(now),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "rules_version": RULES_VERSION,
        "downloads_dir": normalize_path(settings.downloads_dir),
        "target_root": normalize_path(settings.target_root),
        "category_selection": selection,
        "recursive": bool(scan.recursive),
        "scan_scope": list(scan.scan_scope),
        "selected_scope": list(scan.selected_scope),
        "counts": {
            "included": scan.included_count,
            "selected": len(operations),
            "excluded": scan.excluded_count,
            "sensitive_skipped": scan.sensitive_count,
            "top_level_entries": scan.top_level_entry_count,
            "nested_files": sum(1 for op in operations if "/" in str(op.get("rel_path", ""))),
            "scanned_dirs": scan.visited_dir_count,
        },
        "category_totals": {
            "counts": dict(scan.category_counts),
            "sizes": dict(scan.category_sizes),
        },
        "excluded": _excluded_payload(scan),
        "operations": operations,
        "plan_hash_alg": PLAN_HASH_ALG,
        # 目录身份（计划生成时记录；不参与 plan_hash：目录可能在被替换后仍应被拒绝，
        # 因此它属于「运行期绑定的上下文」，由 verify_plan_binding 校验）
        "dir_identities": capture_dir_identities(settings, operations),
    }
    from .directory_archive import prepare_archive_plan
    try:
        prepare_archive_plan(settings, plan, scan, archive_emptied_dirs)
    except (OSError, ValueError) as exc:
        raise PlanError(str(exc)) from exc
    plan["plan_hash"] = compute_plan_hash(plan)
    return PlanBuild(plan=plan, scan=scan)


def plan_path(settings: Settings, plan_id: str) -> str:
    return os.path.join(settings.plans_dir, f"{plan_id}.json")


def assert_new_move_admissible(settings: Settings, plan: dict[str, Any], *, structural: bool = True) -> None:
    """新分类移动的纯字符串准入；历史 undo/recover 不调用此函数。

    必须在路径元数据校验、确认写入和能力探测之前执行。
    不信任配置/计划自述的版本；当前版本也逐项检查实际名称。
    """
    if plan.get("schema_version") == 4:
        from .remaining_archive import validate_structure
        validate_structure(settings, plan)
        if not plan['remaining_selection']:
            raise MoveAdmissionRefused('尚未明确勾选任何剩余项目，不能确认或执行归档。')
        return
    if plan.get("schema_version") != 3 or plan.get("rules_version") != RULES_VERSION:
        raise MoveAdmissionRefused("计划的筛查规则已过期，请重新生成预览并确认；历史撤销/恢复仍可使用。")
    anchor, target = settings.canonical_downloads_dir, settings.canonical_target_root
    if plan.get("downloads_dir") != anchor or plan.get("target_root") != target:
        raise MoveAdmissionRefused("计划根与当前配置不一致，请重新生成预览并确认。")

    def relative(value: Any, *, empty: bool = False) -> str:
        if (not isinstance(value, str) or (not value and not empty)
                or "\x00" in value
                or (value and any(p in ("", ".", "..") for p in value.split("/")))):
            raise MoveAdmissionRefused("计划相对路径或范围非法，请重新生成预览并确认。")
        return value

    scopes = []
    for key in ("selected_scope", "scan_scope"):
        values = plan.get(key)
        if not isinstance(values, list) or "" not in values:
            raise MoveAdmissionRefused("计划缺少有效范围，请重新生成预览并确认。")
        scopes.append([relative(v, empty=True) for v in values])
    selected_scope, scan_scope = scopes
    recursive = plan.get("recursive")
    if not isinstance(recursive, bool) or (not recursive and any(selected_scope + scan_scope)):
        raise MoveAdmissionRefused("计划递归开关与范围不一致，请重新生成预览并确认。")
    categories = plan.get("category_selection")
    if (not isinstance(categories, list) or any(c not in CATEGORIES for c in categories)
            or categories != sorted_selection(categories)):
        raise MoveAdmissionRefused("计划类别集合非法，请重新生成预览并确认。")
    operations = plan.get("operations")
    if not isinstance(operations, list):
        raise MoveAdmissionRefused("计划操作集合非法，请重新生成预览并确认。")
    relatives = selected_scope + scan_scope
    if any(is_sensitive_relative_path(rel) for rel in relatives):
        raise MoveAdmissionRefused("计划含当前筛查规则应跳过的项，请重新生成预览并确认。")
    for op in operations:
        if not isinstance(op, dict):
            raise MoveAdmissionRefused("计划操作非法，请重新生成预览并确认。")
        rel_path = relative(op.get("rel_path"))
        rel_dir, _, name = rel_path.rpartition("/")
        category = op.get("category")
        if category not in categories or (rel_dir and not (
                recursive and any(s and (rel_dir == s or rel_dir.startswith(s + "/"))
                                  for s in selected_scope))):
            raise MoveAdmissionRefused("计划操作不在显式范围或类别集合内，请重新生成预览并确认。")
        relatives = [rel_path]
        for field, root in (("source", anchor), ("dest", target)):
            path = op.get(field)
            # 边界由可信配置决定；保留全部实际父段，根外直接拒绝。
            if not isinstance(path, str) or not path.startswith(root + "/"):
                raise PlanError("计划路径超出当前配置根；不允许路径改写或符号链接，请重新生成预览并确认。")
            relatives.append(relative(path[len(root) + 1:]))
        if any(is_sensitive_relative_path(rel) for rel in relatives):
            raise MoveAdmissionRefused("计划含当前筛查规则应跳过的项，请重新生成预览并确认。")
        # 旧 v2 计划没有子类字段，必须继续按其持久化旧去向校验，供 undo/recover
        # 使用；新规则计划则要求顶层文档带有明确子类目标。
        legacy_layout = plan.get("schema_version") == 2 and plan.get("rules_version") == "milestone4-privacy-r11"
        expected_subcategory = op.get("document_subcategory")
        if legacy_layout and expected_subcategory is None:
            expected_dest = target + "/" + category + "/" + rel_path
            if (op["source"] != unicodedata.normalize("NFC", anchor + "/" + rel_path)
                    or op["dest"] != unicodedata.normalize("NFC", expected_dest)):
                raise PlanError(
                    f"{op_id}: 旧计划目标与持久化相对层级不一致：{op['dest']!r} != {expected_dest!r}"
                )
            continue
        legacy_layout = plan.get("schema_version") == 2 and plan.get("rules_version") == "milestone4-privacy-r11"
        if not legacy_layout:
            from .rules import (DOCUMENT_SUBCATEGORIES,
                                document_subcategory as expected_document_subcategory,
                                normalize_extension)
            actual_subcategory = expected_document_subcategory(normalize_extension(name))
            if category == "文档资料" and not rel_dir and (
                    not isinstance(expected_subcategory, str)
                    or not expected_subcategory
                    or expected_subcategory not in DOCUMENT_SUBCATEGORIES
                    or actual_subcategory != expected_subcategory):
                raise MoveAdmissionRefused("顶层文档必须绑定有效子类，请重新生成预览并确认。")
            if (expected_subcategory is not None and (
                    category != "文档资料" or rel_dir
                    or expected_subcategory not in DOCUMENT_SUBCATEGORIES
                    or actual_subcategory != expected_subcategory)):
                raise MoveAdmissionRefused("计划文档子类与扩展名或范围不一致，请重新生成预览并确认。")
        if category != "文档资料" or rel_dir:
            if expected_subcategory is not None:
                raise MoveAdmissionRefused("非顶层文档操作不能包含文档子类，请重新生成预览并确认。")
        expected_dest = target + "/" + category + "/"
        if category == "文档资料" and not rel_dir and expected_subcategory:
            expected_dest += expected_subcategory + "/"
        expected_dest += rel_path
        if (op["source"] != unicodedata.normalize("NFC", anchor + "/" + rel_path)
                or op["dest"] != unicodedata.normalize("NFC", expected_dest)):
            raise PlanError("计划路径与相对层级不一致；不允许路径改写或符号链接，请重新生成预览并确认。")

    from .directory_archive import validate_structure
    try:
        if structural:
            validate_structure(settings, plan)
    except PlanError as exc:
        if "子类" in str(exc):
            raise MoveAdmissionRefused(str(exc)) from exc
        raise


def _reject_new_move_links(settings: Settings, plan: dict[str, Any]) -> None:
    """从原始段逐层检查，使用目录 fd 防止父段替换后跟随链接。

    最后一段也检查；缺失项保留既有执行时跳过/建目录语义。绝不 readlink。
    """
    paths = [settings.canonical_downloads_dir, settings.canonical_target_root]
    paths.extend(op[field] for op in plan["operations"] + plan.get("directory_operations", []) for field in ("source", "dest"))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        for path in paths:
            fd = os.open("/", flags)
            try:
                parts = path.split("/")[1:]
                for index, part in enumerate(parts):
                    try:
                        info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        break
                    if stat.S_ISLNK(info.st_mode):
                        raise PlanError("计划路径包含符号链接，请重新生成预览并确认。")
                    if index < len(parts) - 1:
                        next_fd = os.open(part, flags, dir_fd=fd)
                        os.close(fd)
                        fd = next_fd
            finally:
                os.close(fd)
    except OSError as exc:
        raise MoveAdmissionRefused("无法不跟随链接核对计划路径，请重新生成预览并确认。") from exc


def public_plan_view(plan: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """只为展示生成遮蔽副本；原不可变历史计划保持原样。"""
    roots = (settings.downloads_dir, settings.target_root, settings.state_dir,
             settings.canonical_downloads_dir, settings.canonical_target_root,
             settings.canonical_state_dir)
    def public(value):
        if isinstance(value, str):
            return redact_private_text(value, roots=roots)
        if isinstance(value, list):
            return [public(v) for v in value]
        if isinstance(value, dict):
            return {public(k): public(v) for k, v in value.items()}
        return value
    view = public(plan)
    # 指纹、编号与版本不是文件名；数字模式不能把合法指纹当作私密号码。
    for key in ("plan_id", "plan_hash", "plan_hash_alg", "rules_version", "created_at"):
        if key in plan:
            view[key] = plan[key]
    return view


def write_plan(settings: Settings, plan: dict[str, Any]) -> str:
    """排他新建写入计划文件；同名存在即换新 id，绝不覆盖。"""
    os.makedirs(settings.plans_dir, mode=0o700, exist_ok=True)
    plan_id = str(plan["plan_id"])
    for attempt in range(8):
        path = plan_path(settings, plan_id)
        payload = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            plan_id = new_plan_id()
            plan["plan_id"] = plan_id
            if plan.get("schema_version") == 4:
                from .remaining_archive import rebase_unpublished
                rebase_unpublished(settings, plan, plan_id)
            elif plan.get("schema_version")==3:
                old_root=plan['archive']['relative_root']
                new_root=f"{settings.target_parent_name}/已归档/{plan_id}/空文件夹"
                plan['archive'].update(batch_id=plan_id,relative_root=new_root)
                for op in plan['directory_operations']:
                    op['dest']=plan['downloads_dir']+'/'+new_root+'/'+op['op_id']+'/'+op['rel_path'].rsplit('/',1)[-1]
                # This is still an unpublished preview; capture the NEW slot chain,
                # never transplant another batch's container identity.
                plan['dir_identities']['archive_dirs']={}
                from .directory_archive import chains,identity,raw_guard
                for op in plan['directory_operations']:
                    rel=os.path.relpath(os.path.dirname(op['dest']),plan['target_root'])
                    for parent in chains(rel)[1:]:
                        path=plan['target_root']+'/'+parent
                        raw_guard(plan['downloads_dir'],path)
                        plan['dir_identities']['archive_dirs'][parent]=identity(path)
                plan['plan_hash']=compute_plan_hash(plan)
            continue
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        return path
    raise PlanError("无法为计划分配唯一的文件名（多次冲突）")


def load_plan(settings: Settings, plan_id: str) -> dict[str, Any]:
    path = plan_path(settings, plan_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            plan = json.load(fh)
    except FileNotFoundError as exc:
        raise PlanError(f"计划不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"计划文件不是合法 JSON：{path}") from exc
    if not isinstance(plan, dict):
        raise PlanError(f"计划文件顶层必须是 JSON 对象：{path}")
    if str(plan.get("plan_id")) != plan_id:
        raise PlanError("计划文件内的 plan_id 与文件名不一致")
    return plan


def assert_plan_format_supported(plan: dict[str, Any]) -> None:
    """计划格式版本必须与当前实现兼容（旧格式明确拒绝，不静默改语义）。"""
    version = plan.get("schema_version")
    from .remaining_archive import RULES_VERSION as REMAINING_RULES
    supported = {2: {"milestone4-privacy-r11", "milestone5-document-subcategories-r1"}, 3: {RULES_VERSION}, 4: {REMAINING_RULES}}
    if version not in supported or plan.get("rules_version") not in supported[version]:
        raise PlanError(
            f"计划格式版本不兼容（计划 v{version}，当前需要 v{PLAN_SCHEMA_VERSION}）："
            "本版本引入子文件夹递归与相对层级，旧计划语义不同。"
            "请重新「生成预览」并确认，不要沿用旧计划。"
        )


def load_verified_plan(
    settings: Settings, plan_id: str, *, verify_binding: bool = False,
    for_new_move: bool = False, display_only: bool = False,
) -> dict[str, Any]:
    """读取计划、校验格式版本与指纹；``verify_binding=True`` 时绑定当前配置。"""
    plan = load_plan(settings, plan_id)
    if for_new_move and plan.get("schema_version") != 4 and (plan.get("schema_version") != 3 or plan.get("rules_version") != RULES_VERSION):
        raise MoveAdmissionRefused("计划的筛查规则已过期，请重新生成预览并确认；历史撤销/恢复仍可使用。")
    assert_plan_format_supported(plan)
    if display_only and (verify_binding or for_new_move):
        raise PlanError("仅展示校验不能用于移动或绑定准入")
    # 哈希契约的 realpath 也会读元数据，新移动必须在它之前筛查名称。
    # 历史 undo/recover 保留原路径与完整指纹校验，不受规则过期影响。
    if for_new_move:
        assert_new_move_admissible(settings,plan,structural=False)
        for field in ('source_dirs','dest_dirs','archive_dirs'):
            normalized_dir_identity_map(plan.get('dir_identities',{}).get(field,{}) or {},field)
        _reject_new_move_links(settings,plan)
    # 新移动路径已绑定规范存储字符串，不再让指纹编码重新跟随链接。
    # 默认历史校验保持原契约；同一规范路径的指纹字节不变。
    if not verify_plan_hash(plan, resolve_paths=not (display_only or for_new_move)):
        raise PlanError(
            f"计划指纹校验失败（plan_body 与 plan_hash 不一致）：{plan_id}；"
            "计划文件可能被修改，拒绝执行。"
        )
    if for_new_move:
        assert_new_move_admissible(settings,plan)
    if verify_binding:
        verify_plan_binding(settings, plan)
    return plan


def check_target_root_containment(downloads_dir: str, target_parent_name: str) -> None:
    """目标根必须是下载目录的直接子目录（按**原始**路径判断，不解析链接）。"""
    if os.path.isabs(target_parent_name) or os.sep in target_parent_name:
        raise PlanError(f"目标父目录名必须是单层名称：{target_parent_name!r}")
    norm_raw = os.path.normpath(os.path.join(downloads_dir, target_parent_name))
    if os.path.dirname(norm_raw) != os.path.normpath(downloads_dir):
        raise PlanError(f"目标根越界（不在下载目录下一层）：{norm_raw}")


def verify_plan_binding(settings: Settings, plan: dict[str, Any]) -> None:
    """载入计划时把它**绑定到当前配置**，并逐项校验路径形状、范围与链接边界。

    校验内容（任一不符即拒绝，且在**创建目标目录或移动之前**）：

    1. **计划格式版本**：只接受当前 ``PLAN_SCHEMA_VERSION``；旧计划（v1，顶层语义）
       明确拒绝并提示重新生成预览，**不静默改变其语义**；
    2. 计划根与当前配置一致，且下载目录/目标根不是链接、身份与生成时一致；
    3. 每个 ``source`` 位于 ``downloads_dir`` 内，其**相对父目录链**必须落在
       ``scan_scope`` 之内（范围之外的源一律拒绝）；
    4. 每个 ``dest`` 严格等于 ``目标根/<固定类别>/<原相对目录链>/<原名>``（不允许改名、
       不允许越出类别、不允许把子目录文件拍到类别根）；
    5. 源与目标的**父目录链**、目标根都不得包含符号链接，且必须在下载根边界内；
    6. 校验**所有已记录的父目录链身份**（不只是最外层根）。
    """
    assert_plan_format_supported(plan)
    if plan.get("schema_version") in (3, 4):
        from .directory_archive import verify_binding
        verify_binding(settings, plan)
        return

    downloads_dir = normalize_path(settings.downloads_dir)
    target_root = normalize_path(settings.target_root)
    if plan.get("downloads_dir") != downloads_dir:
        raise PlanError(
            "计划绑定的下载目录与当前配置不一致："
            f"计划 {plan.get('downloads_dir')!r} vs 当前 {downloads_dir!r}"
        )
    if plan.get("target_root") != target_root:
        raise PlanError(
            "计划绑定的目标根与当前配置不一致："
            f"计划 {plan.get('target_root')!r} vs 当前 {target_root!r}"
        )
    selected = plan.get("category_selection") or []
    unknown = [c for c in selected if c not in CATEGORIES]
    if unknown:
        raise PlanError(f"计划勾选集合含未知类别：{unknown}")
    if list(selected) != sorted_selection(selected):
        raise PlanError("计划勾选集合不是固定的类别顺序")

    scope = [str(item).strip("/") for item in (plan.get("selected_scope", plan.get("scan_scope")) or [])]
    if not scope or "" not in scope:
        raise PlanError("计划缺少有效扫描范围（scan_scope 必须包含顶层）")

    def in_scope(rel_dir: str) -> bool:
        if not rel_dir:
            return True
        return any(
            rel_dir == s or rel_dir.startswith(s + "/") for s in scope if s
        ) or any(s == rel_dir for s in scope)

    # 信任锚点：下载目录（解析其自身的系统级链接），其余路径一律用**原始形式**
    # 相对锚点判定，从而发现「原始路径中的链接段」绕过。
    anchor = normalize_path(settings.downloads_dir).replace("/", os.sep)
    raw_target_root = os.path.join(anchor, settings.target_parent_name)
    for label, path in (
        ("下载目录", settings.downloads_dir),
        ("目标根", settings.target_root),
        ("目标根（原始拼接）", raw_target_root),
    ):
        if os.path.islink(path):
            raise PlanError(f"{label}是符号链接，拒绝执行/撤销：{path}")

    checked_raw: list[tuple[str, str]] = []
    for operation in plan.get("operations", []):
        op_id = str(operation.get("op_id"))
        source = str(operation.get("source"))
        dest = str(operation.get("dest"))
        category = str(operation.get("category"))
        rel_path = str(operation.get("rel_path") or "").strip("/")

        if category not in CATEGORIES or category not in selected:
            raise PlanError(f"{op_id}: 类别不在固定七类内：{category!r}")
        if not rel_path:
            raise PlanError(f"{op_id}: 缺少 rel_path（计划格式 v2 必填）")
        rel_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        name = rel_path.rsplit("/", 1)[-1]
        if name in ("", ".", "..") or "/" in name:
            raise PlanError(f"{op_id}: 源文件名非法：{rel_path!r}")
        if rel_dir and (
            os.path.isabs(rel_dir) or rel_dir == ".." or rel_dir.startswith("../") or "/../" in rel_dir
        ):
            raise PlanError(f"{op_id}: 相对目录链越界：{rel_dir!r}")
        if not in_scope(rel_dir):
            raise PlanError(
                f"{op_id}: 源的相对目录 {rel_dir!r} 不在计划的扫描范围 {scope!r} 内"
            )

        expected_source = normalize_path(
            os.path.join(settings.downloads_dir, *rel_path.split("/"))
        )
        if source != expected_source:
            raise PlanError(
                f"{op_id}: 源路径与 rel_path 不一致：{source!r} != {expected_source!r}"
            )
        if os.path.basename(dest.replace("/", os.sep)) != name:
            raise PlanError(f"{op_id}: 目标文件名与源文件名不一致（不支持改名）：{dest!r}")
        subcategory = operation.get("document_subcategory")
        legacy_layout = plan.get("schema_version") == 2 and plan.get("rules_version") == "milestone4-privacy-r11"
        if not legacy_layout and category == "文档资料" and not rel_dir:
            from .rules import DOCUMENT_SUBCATEGORIES, document_subcategory as expected_document_subcategory, normalize_extension
            expected_subcategory = expected_document_subcategory(normalize_extension(name))
            if (not isinstance(subcategory, str) or not subcategory
                    or subcategory not in DOCUMENT_SUBCATEGORIES
                    or subcategory != expected_subcategory):
                raise PlanError(f"{op_id}: 当前规则顶层文档缺少有效子类绑定")
        if subcategory is not None:
            from .rules import (DOCUMENT_SUBCATEGORIES,
                                document_subcategory as expected_document_subcategory,
                                normalize_extension)
            if category != "文档资料" or rel_dir or subcategory not in DOCUMENT_SUBCATEGORIES:
                raise PlanError(f"{op_id}: 文档子类与来源范围不一致")
            actual_subcategory = expected_document_subcategory(normalize_extension(name))
            if actual_subcategory != subcategory:
                raise PlanError(f"{op_id}: 文档子类与扩展名不一致，请重新生成预览并确认。")
        expected_dest = normalize_path(
            settings.dest_for(category, name, rel_dir, subcategory)
        )
        if dest != expected_dest:
            raise PlanError(
                f"{op_id}: 目标必须是 目标根/固定类别/原相对目录链/原名："
                f"{dest!r} != {expected_dest!r}"
            )
        raw_source = os.path.join(anchor, *rel_path.split("/"))
        raw_subcategory = operation.get("document_subcategory")
        raw_dest_parts = [anchor, settings.target_parent_name, *category.split("/")]
        if raw_subcategory and category == "文档资料" and not rel_dir:
            raw_dest_parts.append(str(raw_subcategory))
        raw_dest = os.path.join(
            *raw_dest_parts,
            *([p for p in rel_dir.split("/") if p] if rel_dir else []), name,
        )
        checked_raw.append((raw_source, raw_dest))

    # 链接边界（在解析链接之前检查原始路径；只检查相对信任锚点之下的段）
    try:
        reject_symlink_segments_between(anchor, raw_target_root)
        for raw_source, raw_dest in checked_raw:
            reject_symlink_segments_between(anchor, raw_source, skip_last=True)
            reject_symlink_segments_between(anchor, raw_dest, skip_last=True)
    except Exception as exc:  # PlanHashError 等
        raise PlanError(f"计划路径包含符号链接或越界：{exc}") from exc

    # 目录身份：下载目录、目标根与**所有已记录父目录链**
    identities = plan.get("dir_identities") or {}
    verify_dir_identity("下载目录", settings.downloads_dir, identities.get("downloads_dir"))
    verify_dir_identity("目标根", settings.target_root, identities.get("target_root"))
    verify_dir_identities_from_plan(settings, plan)


def _dir_identity(path: str) -> list[int] | None:
    from .atomic_move import dir_identity

    if os.path.islink(path) or not os.path.isdir(path):
        return None
    dev, ino = dir_identity(path)
    return [dev, ino]


def capture_dir_identities(
    settings: Settings, operations: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """记录**所需父目录链**的身份（``st_dev``/``st_ino``）。

    除下载目录与目标根，还记录：

    - ``source_dirs``：每个 operation 源文件的父目录链（相对下载目录的路径 → 身份）；
    - ``dest_dirs``：目标父目录链（不存在的目录记录为 ``None``，执行时创建）。

    身份键仅统一为 NFC；文件系统名称、路径分段和 dev/inode 基准不变。

    ``None`` 表示计划生成时该目录不存在；校验时对 ``None`` 只要求
    「现在也不是被替换成链接」，不把「尚未创建」误判为失败。
    """
    out: dict[str, Any] = {
        "downloads_dir": _dir_identity(settings.downloads_dir),
        "target_root": _dir_identity(settings.target_root),
        "source_dirs": {},
        "dest_dirs": {},
    }

    def source_chain(rel_dir: str) -> list[str]:
        chain = [""]
        if rel_dir:
            parts = rel_dir.split("/")
            chain += ["/".join(parts[: i + 1]) for i in range(len(parts))]
        return chain

    for operation in operations or []:
        rel_path = str(operation.get("rel_path") or "")
        rel_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        for rel in source_chain(rel_dir):
            if rel in out["source_dirs"]:
                continue
            abs_dir = settings.downloads_dir if not rel else os.path.join(
                settings.downloads_dir, *rel.split("/")
            )
            out["source_dirs"][rel] = _dir_identity(abs_dir)
        category = str(operation.get("category"))
        subcategory = operation.get("document_subcategory")
        base_label = f"{category}/{subcategory}" if subcategory else category
        label = f"{base_label}/{rel_dir}" if rel_dir else base_label
        for rel in source_chain(label)[1:]:
            out["dest_dirs"][rel] = _dir_identity(
                os.path.join(settings.target_root, *rel.split("/")))
    for field in ("source_dirs", "dest_dirs"):
        out[field] = normalized_dir_identity_map(out[field], field)
    return out


def directory_identity_key(relative_path: str) -> str:
    """与 operation 路径一致的 NFC 身份键；不改分隔符或磁盘名称。"""
    return unicodedata.normalize("NFC", relative_path)


def normalized_dir_identity_map(identities: dict[str, Any], label: str) -> dict[str, Any]:
    """只在内存中兼容旧 v2 原始编码键，拒绝折叠后有歧义的记录。"""
    normalized: dict[str, Any] = {}
    for raw, expected in identities.items():
        if not isinstance(raw, str):
            raise PlanError(f"{label} 目录身份键不是字符串")
        key = directory_identity_key(raw)
        if key in normalized:
            # 即使身份值相同，也不能静默合并两个不同编码的记录。
            raise PlanError(f"{label} 目录身份键 NFC 冲突或歧义：{key}")
        normalized[key] = expected
    return normalized


def verify_dir_identity(label: str, path: str, expected: Any) -> None:
    """目录身份必须与计划中记录的一致（拒绝被替换的目录）。

    - ``expected is None`` 且计划里**没有该记录** → 调用方用 :data:`_MISSING`
      区分；这里对 ``None`` 只要求现在也不是「被替换成链接」。
    """
    if os.path.islink(path):
        raise PlanError(f"{label}是符号链接：{path}")
    if expected is _MISSING:
        return
    if expected is None:
        return
    if not os.path.isdir(path):
        raise PlanError(f"{label}不存在或不是目录：{path}")
    from .atomic_move import dir_identity

    actual = dir_identity(path)
    if tuple(int(x) for x in expected) != actual:
        raise PlanError(f"{label}身份与计划不一致（目录被替换？）：{path}")


class _Missing:
    """哨兵：计划里没有该目录的身份记录（例如目标父目录尚未创建）。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<missing>"


_MISSING = _Missing()


def verify_dir_identities_from_plan(settings: Settings, plan: dict[str, Any]) -> None:
    """校验**所有已记录父目录链**的身份（不只是最外层根）。"""
    identities = plan.get("dir_identities") or {}
    source_dirs = normalized_dir_identity_map(identities.get("source_dirs") or {}, "source_dirs")
    dest_dirs = normalized_dir_identity_map(identities.get("dest_dirs") or {}, "dest_dirs")
    for rel, expected in source_dirs.items():
        abs_dir = settings.downloads_dir if not rel else os.path.join(
            settings.downloads_dir, *rel.split("/")
        )
        verify_dir_identity(f"源父目录 {rel or '(顶层)'}", abs_dir, expected)
    for label, expected in dest_dirs.items():
        if expected is None:
            continue  # 计划时不存在：执行阶段创建，并逐段检查链接
        abs_dir = os.path.join(settings.target_root, *str(label).split("/"))
        verify_dir_identity(f"目标父目录 {label}", abs_dir, expected)


def list_plans(settings: Settings) -> list[str]:
    try:
        names = os.listdir(settings.plans_dir)
    except FileNotFoundError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json"))


# --- 确认记录 -----------------------------------------------------------------
def confirmation_path(settings: Settings, plan_id: str) -> str:
    return os.path.join(settings.confirmations_dir, f"{plan_id}.json")


def write_confirmation(settings: Settings, plan_id: str, plan_hash: str) -> str:
    """写入确认记录（用户确认的是**这一份** plan_hash）。"""
    plan = load_verified_plan(settings, plan_id, for_new_move=True)
    if plan_hash != plan["plan_hash"]:
        raise PlanError("确认指纹与计划不一致")
    verify_plan_binding(settings, plan)
    from .executor import _assert_admissible
    _assert_admissible(settings,plan_id)
    existing=read_confirmation(settings,plan_id)
    if existing is not None:
        if (existing.get("confirmed_plan_hash") != plan_hash or existing.get("archive_enabled") != plan["archive"]["enabled"] or existing.get("plan_schema_version") != plan["schema_version"] or existing.get("rules_version") != plan["rules_version"] or existing.get("plan_hash_alg") != plan["plan_hash_alg"]):
            raise PlanError("既有确认与计划不一致，请重新预览；未改写确认")
        return confirmation_path(settings,plan_id)
    os.makedirs(settings.confirmations_dir, mode=0o700, exist_ok=True)
    path = confirmation_path(settings, plan_id)
    payload = {
        "schema_version": 1,
        "plan_id": plan_id,
        "confirmed_plan_hash": plan_hash,
        "plan_schema_version": plan["schema_version"],
        "rules_version": plan["rules_version"],
        "plan_hash_alg": plan["plan_hash_alg"],
        "archive_enabled": (plan.get("archive") or {}).get("enabled", False),
        "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def read_confirmation(settings: Settings, plan_id: str) -> dict[str, Any] | None:
    path = confirmation_path(settings, plan_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) else None


def drop_confirmation(settings: Settings, plan_id: str) -> None:
    try:
        os.unlink(confirmation_path(settings, plan_id))
    except FileNotFoundError:
        pass
