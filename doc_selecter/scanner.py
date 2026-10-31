"""受控递归扫描。

三种扫描模式：

1. **仅顶层**（默认）：与既有行为完全一致，只处理下载目录顶层的普通文件；
2. **递归**：只对用户**显式选中**的目录递归（``scope_dirs``），未选中的目录整体跳过、
   保持原样；
3. **文件夹选择辅助** ``list_selectable_folders``：给界面用，列出可选的子目录
   （同样跳过目标根、隐藏、包目录、代码项目、敏感目录与空目录）。

所有模式都：**不跟随符号链接、不读取文件正文、不进入被剔除的目录**。
递归权限/I/O 错误与扫描期间变化**如实记录**，绝不把「无法读取」当成「空目录或不存在」。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .atomic_move import FileIdentity
from .plan_hash import path_is_within
from .rules import (
    CATEGORIES,
    EXCLUDED_DIR,
    EXCLUDED_HIDDEN,
    EXCLUDED_LEAF_DIR,
    EXCLUDED_OTHER,
    EXCLUDED_PACKAGE_DIR,
    EXCLUDED_CODE_PROJECT,
    EXCLUDED_SENSITIVE,
    EXCLUDED_SENSITIVE_DIR,
    EXCLUDED_SYMLINK,
    EXCLUDED_UNFINISHED,
    EXCLUSION_LABELS,
    Classification,
    classify_entry,
    is_code_project_dir,
    is_leaf_dir,
    is_package_dir,
    is_sensitive_dir,
    is_sensitive_name,
    is_temp_download,
    normalize_extension,
)

#: 文件夹选择器默认最大深度（防止界面枚举过深目录树）
MAX_FOLDER_DEPTH = 6


@dataclass
class ScannedEntry:
    """一个被纳入整理的普通文件。"""

    rel_path: str            # 相对下载目录的路径，使用 "/" 分隔（顶层文件无分隔符）
    name: str               # 文件名
    classification: Classification
    identity: FileIdentity | None
    mtime_ns: int
    size_bytes: int

    @property
    def rel_dir(self) -> str:
        """相对父目录链（顶层为空字符串）。"""
        return os.path.dirname(self.rel_path)

    @property
    def is_nested(self) -> bool:
        return bool(self.rel_dir)


@dataclass
class ExcludedEntry:
    rel_path: str
    code: str
    label: str
    basis: str


@dataclass
class ScanIssue:
    """递归读取失败 / 扫描期间变化。"""

    rel_path: str
    code: str
    detail: str


@dataclass
class FolderCandidate:
    """文件夹选择器的一项。"""

    rel_path: str
    file_count: int


@dataclass
class ScanResult:
    downloads_dir: str
    scan_scope: list[str] = field(default_factory=list)
    selected_scope: list[str] = field(default_factory=list)
    recursive: bool = False
    included: list[ScannedEntry] = field(default_factory=list)
    excluded: list[ExcludedEntry] = field(default_factory=list)
    sensitive_names: list[str] = field(default_factory=list)  # 保留旧接口；当前扫描不存名称
    sensitive_dir_count: int = 0  # 兼容字段：未知类型敏感项总数，不额外 stat 区分类型
    metadata_failures: list[tuple[str, str]] = field(default_factory=list)
    scan_issues: list[ScanIssue] = field(default_factory=list)
    excluded_primary_counts: dict[str, int] = field(default_factory=dict)
    metadata_failure_counts: dict[str, int] = field(default_factory=dict)
    category_counts: dict[str, int] = field(default_factory=dict)
    category_sizes: dict[str, int] = field(default_factory=dict)
    top_level_entry_count: int = 0
    visited_dir_count: int = 0
    target_root_excluded: bool = False
    added_during_scan: list[str] = field(default_factory=list)
    removed_during_scan: list[str] = field(default_factory=list)

    @property
    def included_count(self) -> int:
        return len(self.included)

    @property
    def total_logical_size(self) -> int:
        return sum(e.size_bytes for e in self.included)

    @property
    def sensitive_count(self) -> int:
        return len(self.sensitive_names) + self.sensitive_dir_count

    @property
    def failure_count(self) -> int:
        return len(self.metadata_failures) + len(self.scan_issues)

    @property
    def excluded_count(self) -> int:
        return sum(self.excluded_primary_counts.values()) + self.failure_count


def _new_result(downloads_dir: str, scope: list[str], recursive: bool) -> ScanResult:
    result = ScanResult(downloads_dir=downloads_dir, scan_scope=scope, recursive=recursive)
    for code in (
        EXCLUDED_SENSITIVE, EXCLUDED_SENSITIVE_DIR, EXCLUDED_SYMLINK, EXCLUDED_HIDDEN,
        EXCLUDED_LEAF_DIR, EXCLUDED_PACKAGE_DIR, EXCLUDED_CODE_PROJECT, EXCLUDED_DIR,
        EXCLUDED_UNFINISHED, EXCLUDED_OTHER,
    ):
        result.excluded_primary_counts.setdefault(code, 0)
    for category in CATEGORIES:
        result.category_counts[category] = 0
        result.category_sizes[category] = 0
    return result


def _exclude(
    result: ScanResult,
    *,
    rel_path: str,
    code: str,
    basis: str,
    sensitive: bool = False,
) -> None:
    result.excluded_primary_counts[code] = result.excluded_primary_counts.get(code, 0) + 1
    if sensitive:
        result.sensitive_dir_count += 1
        return  # 疑似敏感项：类型未知，只计数量，不记录名称/路径
    result.excluded.append(
        ExcludedEntry(
            rel_path=rel_path,
            code=code,
            label=EXCLUSION_LABELS.get(code, code),
            basis=basis,
        )
    )


def normalize_scope(scope_dirs) -> list[str]:
    """把用户选择的目录集合规范成排序、去重的相对路径列表（含隐式顶层 ``""``）。

    只做**选择项本身**的规范化；展开到子孙目录由扫描过程完成
    （``scan_scope`` 会把实际进入的目录写进 ``ScanResult.scan_scope``）。
    """
    out: set[str] = {""}
    for raw in scope_dirs or ():
        text = str(raw).strip()
        if os.path.isabs(text) or text.startswith("/"):
            raise ValueError(f"扫描范围不能是绝对路径：{raw!r}")
        rel = text.strip("/")
        if not rel or rel == ".":
            continue
        parts: list[str] = []
        for segment in rel.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                raise ValueError(f"扫描范围不能包含 .. 越界段：{raw!r}")
            parts.append(segment)
        if not parts:
            continue
        out.add("/".join(parts))
    return sorted(out)


def scope_is_top_level_only(scope_dirs) -> bool:
    return normalize_scope(scope_dirs) == [""]


def _in_selected_scope(rel_child: str, selected: list[str]) -> bool:
    """``rel_child`` 是否位于任一**选中目录**之下（含自身）。"""
    for picked in selected:
        if rel_child == picked or rel_child.startswith(picked + "/"):
            return True
    return False


def _classify_dir(name: str, *, legacy: bool = False):
    """判定一个目录条目：返回 ``(是否可进入, 原因码, 说明)``。"""
    if is_sensitive_dir(name, legacy=legacy):
        return False, EXCLUDED_SENSITIVE_DIR, "疑似敏感目录名（保守筛查；仅计数量，不记录名称）"
    if is_package_dir(name):
        return False, EXCLUDED_PACKAGE_DIR, "包目录（.app/.framework 等）：整体跳过，绝不进入"
    if name.startswith("."):
        return False, EXCLUDED_HIDDEN, "隐藏目录（名称以 . 开头）：整体跳过"
    return True, "", ""


def scan_scope(
    downloads_dir: str,
    *,
    target_parent_name: str,
    scope_dirs=None,
    recursive: bool = False,
    expected_dir_identity: tuple[int, int] | None = None,
    detect_changes: bool = True,
    _max_depth: int = 32,
    cancel_event=None,
) -> ScanResult:
    """按用户选择的范围扫描并分类。

    ``recursive=False``：只处理顶层普通文件（既有默认行为）。
    ``recursive=True``：对 ``scope_dirs`` 中列出的目录递归；未选中的目录整体跳过。
    """
    scope = normalize_scope(scope_dirs)
    if not recursive:
        scope = [""]
    # 敏感选择项不进入任何持久化或展示范围。
    scope = [p for p in scope if not any(is_sensitive_dir(x) for x in p.split("/"))]
    result = _new_result(downloads_dir, scope, recursive)
    result.selected_scope = list(scope)

    if os.path.islink(downloads_dir):
        raise ValueError(f"下载目录是符号链接，拒绝扫描：{downloads_dir}")
    if not os.path.isdir(downloads_dir):
        raise FileNotFoundError(f"下载目录不存在：{downloads_dir}")

    dir_st = os.lstat(downloads_dir)
    if expected_dir_identity is not None and (dir_st.st_dev, dir_st.st_ino) != expected_dir_identity:
        raise OSError("下载目录身份与预期不符（目录被替换？）")

    anchor = os.path.realpath(downloads_dir)
    target_root = os.path.join(downloads_dir, target_parent_name)
    target_identity: tuple[int, int] | None = None
    try:
        tr_st = os.lstat(target_root)
        if not os.path.islink(target_root) and os.path.isdir(target_root):
            target_identity = (tr_st.st_dev, tr_st.st_ino)
    except FileNotFoundError:
        target_identity = None

    def _is_target_root(rel_dir: str, name: str, path: str) -> bool:
        if rel_dir == "" and name == target_parent_name:
            try:
                st = os.lstat(path)
            except OSError:
                return False
            if target_identity is None:
                return True
            return (st.st_dev, st.st_ino) == target_identity
        return False

    selected = [p for p in scope if p]  # 用户显式选择（不含隐式顶层）
    entered: list[str] = []

    def cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled("检查已取消")

    def walk(rel_dir: str, depth: int) -> None:
        cancelled()
        authorized = not rel_dir or _in_selected_scope(rel_dir, selected)
        abs_dir = os.path.join(downloads_dir, rel_dir) if rel_dir else downloads_dir
        result.visited_dir_count += 1
        if rel_dir and authorized:
            entered.append(rel_dir)
        try:
            with os.scandir(abs_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except FileNotFoundError:
            result.scan_issues.append(
                ScanIssue(rel_path=rel_dir, code="DirectoryMissing", detail="扫描期间目录消失")
            )
            return
        except OSError as exc:
            # 权限/I/O 错误：如实记录，绝不当成空目录
            result.scan_issues.append(
                ScanIssue(rel_path=rel_dir, code=type(exc).__name__, detail=type(exc).__name__)
            )
            return

        subdirs: list[tuple[str, str, frozenset[str]]] = []
        for entry in entries:
            cancelled()
            name = entry.name
            rel_path = f"{rel_dir}/{name}" if rel_dir else name
            path = os.path.join(abs_dir, name)
            # 在 stat/范围判定之前保护敏感名称（包括读取失败）。
            if is_sensitive_name(name) or is_sensitive_dir(name):
                _exclude(result, rel_path=rel_path, code=EXCLUDED_SENSITIVE,
                         basis="疑似敏感项", sensitive=True)
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                result.metadata_failures.append((rel_path, type(exc).__name__))
                key = type(exc).__name__
                result.metadata_failure_counts[key] = result.metadata_failure_counts.get(key, 0) + 1
                continue

            import stat as statmod

            if statmod.S_ISLNK(st.st_mode):
                _exclude(result, rel_path=rel_path, code=EXCLUDED_SYMLINK,
                         basis="符号链接：不跟随、不移动")
                continue
            if _is_target_root(rel_dir, name, path):
                result.target_root_excluded = True
                continue
            if name.startswith("."):
                _exclude(result, rel_path=rel_path, code=EXCLUDED_HIDDEN,
                         basis="隐藏项（名称以 . 开头）：不纳入分类")
                continue
            if statmod.S_ISDIR(st.st_mode):
                rel_child = f"{rel_dir}/{name}" if rel_dir else name
                transit = any(p.startswith(rel_child + "/") for p in selected)
                if not recursive or not (_in_selected_scope(rel_child, selected) or transit):
                    # 未选中（或非递归模式）→ 整体跳过，保持原样
                    _exclude(
                        result, rel_path=rel_path, code=EXCLUDED_DIR,
                        basis="未包含在本次扫描范围（未勾选的目录保持原样）"
                        if recursive else "文件夹：默认不递归（如需整理请开启「包含子文件夹」并选择）",
                    )
                    continue
                enter, code, basis = _classify_dir(name)
                if not enter:
                    _exclude(result, rel_path=rel_path, code=code, basis=basis,
                             sensitive=(code == EXCLUDED_SENSITIVE_DIR))
                    continue
                try:
                    with os.scandir(path) as it:
                        child_names = frozenset(e.name for e in it)
                except OSError as exc:
                    result.scan_issues.append(
                        ScanIssue(rel_path=rel_path, code=type(exc).__name__, detail=type(exc).__name__)
                    )
                    continue
                if is_leaf_dir(child_names):
                    _exclude(result, rel_path=rel_path, code=EXCLUDED_LEAF_DIR,
                             basis="空文件夹（无文件）：整体跳过；不会被移动或删除")
                    continue
                is_project, hit = is_code_project_dir(child_names)
                if is_project:
                    _exclude(result, rel_path=rel_path, code=EXCLUDED_CODE_PROJECT,
                             basis="代码项目目录（直接标记名称命中）：整体跳过")
                    continue
                subdirs.append((rel_child, path, child_names))
                continue

            if not authorized:
                continue  # 穿行父链不授权其文件或兄弟目录
            if not statmod.S_ISREG(st.st_mode):
                _exclude(result, rel_path=rel_path, code=EXCLUDED_OTHER,
                         basis="特殊文件类型（非普通文件）：不纳入分类")
                continue

            if is_temp_download(name):
                _exclude(result, rel_path=rel_path, code=EXCLUDED_UNFINISHED,
                         basis="疑似临时/未完成项：不纳入分类")
                continue

            classification = classify_entry(name, st.st_mode)
            if classification.included:
                result.included.append(
                    ScannedEntry(
                        rel_path=rel_path,
                        name=name,
                        classification=classification,
                        identity=FileIdentity(
                            st_dev=st.st_dev, st_ino=st.st_ino, size_bytes=st.st_size,
                            mtime_ns=st.st_mtime_ns, mode_type="regular",
                        ),
                        mtime_ns=st.st_mtime_ns,
                        size_bytes=st.st_size,
                    )
                )
                category = classification.category or CATEGORIES[-1]
                result.category_counts[category] = result.category_counts.get(category, 0) + 1
                result.category_sizes[category] = result.category_sizes.get(category, 0) + st.st_size
            else:
                code = classification.exclusion_code or EXCLUDED_OTHER
                if code == EXCLUDED_SENSITIVE:
                    _exclude(result, rel_path=rel_path, code=code,
                             basis="疑似敏感项", sensitive=True)
                else:
                    _exclude(result, rel_path=rel_path, code=code, basis=classification.basis)

        if depth >= _max_depth:
            result.scan_issues.append(
                ScanIssue(rel_path=rel_dir, code="MaxDepth", detail=f"超过最大深度 {_max_depth}")
            )
            return
        for rel_child, path, _names in subdirs:
            walk(rel_child, depth + 1)

    walk("", 0)
    # 展开后的实际范围：顶层 + 所有真正进入过的子目录（排序、去重）
    result.scan_scope = sorted({""} | set(entered))
    cancelled()
    result.top_level_entry_count = _count_toplevel(downloads_dir, result)
    if detect_changes:
        _check_changes(downloads_dir, result, target_parent_name=target_parent_name,
                       cancel_event=cancel_event)
    return result


def scan_top_level(downloads_dir: str, *, target_parent_name: str, **kwargs) -> ScanResult:
    """兼容入口：仅顶层扫描（既有默认行为）。"""
    kwargs.pop("scope_dirs", None)
    kwargs.pop("recursive", None)
    return scan_scope(
        downloads_dir, target_parent_name=target_parent_name, recursive=False, **kwargs
    )


def _count_toplevel(downloads_dir: str, result: ScanResult) -> int:
    try:
        with os.scandir(downloads_dir) as it:
            return sum(1 for _ in it)
    except OSError as exc:
        result.scan_issues.append(
            ScanIssue(rel_path="", code=type(exc).__name__, detail=type(exc).__name__)
        )
        return 0


def _observed_paths(result: ScanResult) -> set[str]:
    paths = {e.rel_path for e in result.included}
    paths |= {e.rel_path for e in result.excluded}
    paths |= {name for name, _ in result.metadata_failures}
    paths |= {issue.rel_path for issue in result.scan_issues}
    return paths


class ScanCancelled(RuntimeError):
    """可协作取消；已进入的系统 I/O 只能等待其返回。"""


def _check_changes(downloads_dir: str, result: ScanResult, *,
                   target_parent_name: str, cancel_event=None) -> None:
    """复查复用安全扫描；不越过范围和剔除边界。"""
    second = scan_scope(downloads_dir, target_parent_name=target_parent_name,
                        scope_dirs=result.selected_scope, recursive=result.recursive,
                        detect_changes=False, cancel_event=cancel_event)
    before, after = _observed_paths(result), _observed_paths(second)
    result.added_during_scan = sorted(after - before)
    result.removed_during_scan = sorted(before - after)
    result.scan_issues.extend(second.scan_issues)


def list_selectable_folders(
    downloads_dir: str,
    *,
    target_parent_name: str,
    max_depth: int = MAX_FOLDER_DEPTH,
) -> list[FolderCandidate]:
    """列出可作为递归范围的子目录（供界面文件夹选择器使用）。

    与扫描同一套剔除规则：目标根、隐藏、包目录、代码项目、敏感目录、空目录都不列出。
    """
    out: list[FolderCandidate] = []
    target_root = os.path.join(downloads_dir, target_parent_name)
    try:
        target_identity = (
            os.lstat(target_root).st_ino if os.path.isdir(target_root) else None
        )
    except OSError:
        target_identity = None

    def walk(rel_dir: str, depth: int) -> None:
        if depth > max_depth:
            return
        abs_dir = os.path.join(downloads_dir, rel_dir) if rel_dir else downloads_dir
        try:
            with os.scandir(abs_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if is_sensitive_dir(name):
                continue  # 名称检查先于任何类型/元数据检查
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            if rel_dir == "" and name == target_parent_name:
                continue
            if is_sensitive_dir(name) or is_package_dir(name) or name.startswith("."):
                continue
            rel_child = f"{rel_dir}/{name}" if rel_dir else name
            path = os.path.join(abs_dir, name)
            try:
                with os.scandir(path) as it:
                    names = frozenset(e.name for e in it)
            except OSError:
                continue
            if is_leaf_dir(names):
                continue
            is_project, _hit = is_code_project_dir(names)
            if is_project:
                continue
            file_count = _count_files(path, max_depth=max_depth - depth - 1,
                                      target_parent_name=target_parent_name)
            out.append(FolderCandidate(rel_path=rel_child, file_count=file_count))

    walk("", 0)
    return out


def _count_files(root: str, *, max_depth: int, target_parent_name: str = "下载分类") -> int:
    total = 0
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:
        path, depth = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            continue
        if is_code_project_dir(frozenset(e.name for e in entries))[0]:
            continue
        for entry in entries:
            if is_sensitive_dir(entry.name):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    enter, _, _ = _classify_dir(entry.name)
                    if depth < max_depth and enter and entry.name != target_parent_name:
                        stack.append((os.path.join(path, entry.name), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    if not is_sensitive_name(entry.name) and not entry.name.startswith(".") and not is_temp_download(entry.name):
                        total += 1
            except OSError:
                continue
    return total


class CollectedFiles(dict):
    """保留 dict 接口，同时传递部分覆盖与读取问题。"""
    def __init__(self, scan):
        super().__init__((e.rel_path, e.identity) for e in scan.included if e.identity)
        self.scan_issues = list(scan.scan_issues)
        self.scan_issues.extend(ScanIssue(p, code, code) for p, code in scan.metadata_failures)
        self.temp_files = [e.rel_path for e in scan.excluded if e.code == EXCLUDED_UNFINISHED]


def collect_files(downloads_dir: str, *, target_parent_name: str, scope_dirs=None,
                  recursive: bool = False, cancel_event=None) -> CollectedFiles:
    scan = scan_scope(downloads_dir, target_parent_name=target_parent_name,
                      scope_dirs=scope_dirs, recursive=recursive,
                      detect_changes=False, cancel_event=cancel_event)
    return CollectedFiles(scan)


def collect_temp_files(downloads_dir: str, *, target_parent_name: str, scope_dirs=None,
                       recursive: bool = False, issues=None, cancel_event=None) -> list[str]:
    """与普通文件共用扫描；调用者可接收结构化读取问题。"""
    files = collect_files(downloads_dir, target_parent_name=target_parent_name,
                          scope_dirs=scope_dirs, recursive=recursive, cancel_event=cancel_event)
    if issues is not None:
        issues.extend(files.scan_issues)
    elif files.scan_issues:
        raise OSError("临时项检查覆盖不完整（读取失败）")
    return files.temp_files


def snapshot_downloads(downloads_dir: str, target_parent_name: str) -> dict[str, tuple]:
    """返回顶层快照 ``{name: (size, mtime_ns, st_ino)}``，用于断言「预览只读」。

    这是**弱信号**：不能证明目录从未被访问，也不能证明期间没有写入后恢复原状。
    """
    out: dict[str, tuple] = {}
    with os.scandir(downloads_dir) as it:
        for entry in it:
            st = entry.stat(follow_symlinks=False)
            out[entry.name] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return out
