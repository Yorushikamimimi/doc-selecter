"""配置加载与安全闸门。

**硬规则**：所有真实运行都必须显式提供沙盒/工作路径。缺少配置时一律拒绝运行，
绝不回退到真实 ``~/Downloads``。

路径来源（优先级从高到低）：

1. 环境变量 ``DOCSELECTER_TEST_ROOT``：指向一个**测试专用沙盒**。此时
   ``downloads_dir = <root>/Downloads``、``state_dir = <root>/state``，
   ``config.json`` 被完全忽略。该变量不出现在正常使用说明中。
2. ``config.json``：用户显式编写的配置。

两处都不存在 → 抛 :class:`ConfigError`，拒绝运行。
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from . import RULES_VERSION
from .rules import CATEGORIES

TEST_ROOT_ENV = "DOCSELECTER_TEST_ROOT"

DEFAULT_TARGET_PARENT_NAME = "下载分类"
DEFAULT_CONFIG_FILENAME = "config.json"

#: 本工具自身的实现目录（用于「受保护路径」判定：绝不允许把文件移进实现仓库）
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(PACKAGE_DIR)


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


@dataclass(frozen=True)
class Settings:
    downloads_dir: str
    state_dir: str
    target_parent_name: str
    categories: tuple[str, ...] = CATEGORIES
    rules_version: str = RULES_VERSION
    same_filesystem_only: bool = True
    on_name_conflict: str = "skip"
    move_primitive: str = "rename_excl"
    sandboxed: bool = False
    source: str = "config.json"
    raw: dict[str, Any] = field(default_factory=dict)
    # 仅从本次可信配置捕获系统路径别名；计划字段不能提供或扩大这些锚点。
    canonical_downloads_dir: str = field(init=False, repr=False)
    canonical_state_dir: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for field_name, path in (("canonical_downloads_dir", self.downloads_dir),
                                 ("canonical_state_dir", self.state_dir)):
            canonical = unicodedata.normalize("NFC", os.path.realpath(_norm_abs(path)))
            object.__setattr__(self, field_name, canonical)

    @property
    def canonical_target_root(self) -> str:
        # 不解析目标链：计划之后出现的链接必须由新移动入口拒绝。
        return unicodedata.normalize("NFC", os.path.join(self.canonical_downloads_dir, self.target_parent_name))

    # --- 派生路径 -----------------------------------------------------------
    @property
    def target_root(self) -> str:
        return os.path.join(self.downloads_dir, self.target_parent_name)

    @property
    def plans_dir(self) -> str:
        return os.path.join(self.state_dir, "plans")

    @property
    def confirmations_dir(self) -> str:
        return os.path.join(self.state_dir, "confirmations")

    @property
    def journals_dir(self) -> str:
        return os.path.join(self.state_dir, "journals")

    @property
    def undos_dir(self) -> str:
        return os.path.join(self.state_dir, "undos")

    @property
    def plan_state_dir(self) -> str:
        return os.path.join(self.state_dir, "plan_state")

    @property
    def lock_path(self) -> str:
        return os.path.join(self.state_dir, "lock")

    def category_dir(self, category: str) -> str:
        return os.path.join(self.target_root, category)

    def dest_for(self, category: str, filename: str, rel_dir: str = "",
                 document_subcategory: str | None = None, *, subfolder: str | None = None) -> str:
        """目标路径：``目标根/<类别>/<相对目录链>/<原文件名>``。

        ``rel_dir`` 是相对下载目录的父目录链（``""`` 表示顶层文件，维持既有布局）。
        """
        rel_dir = (rel_dir or "").strip().strip("/")
        parts = [self.target_root, category]
        if not rel_dir and subfolder:
            parts.append(subfolder)
        elif category == "文档资料" and not rel_dir and document_subcategory:
            parts.append(document_subcategory)
        if rel_dir:
            parts.extend(part for part in rel_dir.split("/") if part)
        parts.append(filename)
        return os.path.join(*parts)

    def as_public_dict(self) -> dict[str, Any]:
        """不含敏感信息、可用于展示/记录的配置快照。"""
        return {
            "downloads_dir": self.downloads_dir,
            "state_dir": self.state_dir,
            "target_root": self.target_root,
            "target_parent_name": self.target_parent_name,
            "categories": list(self.categories),
            "rules_version": self.rules_version,
            "same_filesystem_only": self.same_filesystem_only,
            "on_name_conflict": self.on_name_conflict,
            "move_primitive": self.move_primitive,
            "sandboxed": self.sandboxed,
            "source": self.source,
        }


def _norm_abs(path: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def _under(child: str, parent: str) -> bool:
    child = os.path.normpath(child)
    parent = os.path.normpath(parent)
    return child == parent or child.startswith(parent + os.sep)


def check_protected_paths(downloads_dir: str, state_dir: str, target_root: str) -> None:
    """受保护路径检查：拒绝把工作路径放进本工具的实现仓库。"""
    for label, path in (
        ("下载目录", downloads_dir),
        ("状态目录", state_dir),
        ("目标根目录", target_root),
    ):
        if _under(path, PROJECT_DIR):
            raise ConfigError(
                f"{label}位于本工具的实现目录内（{path}）。"
                "为避免把下载文件移进实现仓库，拒绝启动。"
            )


def check_dir_pair(downloads_dir: str, state_dir: str) -> None:
    """下载目录与状态目录不得互相包含（状态目录写日志，不能被自己扫描到）。"""
    if _under(state_dir, downloads_dir):
        raise ConfigError(
            f"状态目录位于下载目录内（{state_dir}），预览会把状态文件当作待分类文件。拒绝启动。"
        )
    if _under(downloads_dir, state_dir):
        raise ConfigError(f"下载目录位于状态目录内（{downloads_dir}）。拒绝启动。")


def _validate_target_parent_name(name: object) -> str:
    """目标父目录名必须是**合法的单层目录名**。

    拒绝：空、``.``/``..``、任何路径分隔符（``/``、``\\``）、控制字符、
    以 ``.`` 开头（隐藏目录）、以及任何会逃出下载目录的形式。
    """
    if not isinstance(name, str) or not name:
        raise ConfigError("target_parent_name 必须是非空字符串")
    if name in (".", ".."):
        raise ConfigError(f"target_parent_name 不能是 {name!r}")
    if name.startswith("."):
        raise ConfigError(f"target_parent_name 不能以 . 开头（隐藏目录）：{name!r}")
    if "/" in name or "\\" in name or os.sep in name or (os.altsep and os.altsep in name):
        raise ConfigError(
            f"target_parent_name 必须是单层目录名，不能包含路径分隔符：{name!r}"
        )
    if any(ord(ch) < 32 or ch in "\x7f" for ch in name):
        raise ConfigError(f"target_parent_name 不能包含控制字符：{name!r}")
    return name


def check_target_root_containment(downloads_dir: str, target_parent_name: str) -> None:
    """目标根必须是下载目录的**直接子目录**：在解析链接**之前**对原始拼接路径校验。

    这样 ``target_parent_name=".."``、``"../Else"``、``"/abs"`` 等形式在
    配置加载阶段即被拒绝，绝不进入计划或移动流程。
    """
    if os.path.isabs(target_parent_name):
        raise ConfigError(f"target_parent_name 不能是绝对路径：{target_parent_name!r}")
    joined_raw = os.path.join(downloads_dir, target_parent_name)
    norm_raw = os.path.normpath(joined_raw)
    expected_parent = os.path.normpath(downloads_dir)
    if os.path.dirname(norm_raw) != expected_parent:
        raise ConfigError(
            f"目标根必须严格位于下载目录的下一层：{downloads_dir} + {target_parent_name!r}"
            f" → {norm_raw}"
        )
    if norm_raw == expected_parent:
        raise ConfigError("目标根不能等于下载目录本身")


def check_not_symlink(label: str, path: str) -> None:
    """拒绝符号链接：链接会把写入重定向到下载目录之外。"""
    if os.path.islink(path):
        raise ConfigError(f"{label}是符号链接，拒绝运行（会成为链接绕过）：{path}")


def _validate_common(cfg: dict[str, Any]) -> None:
    cfg["target_parent_name"] = _validate_target_parent_name(
        cfg.get("target_parent_name", DEFAULT_TARGET_PARENT_NAME)
    )
    if cfg.get("same_filesystem_only") is not True:
        raise ConfigError("same_filesystem_only 必须为 true（MVP 不支持跨卷搬运）")
    if cfg.get("on_name_conflict", "skip") != "skip":
        raise ConfigError("on_name_conflict 只允许 \"skip\"（不提供覆盖或自动改名）")
    if cfg.get("move_primitive", "rename_excl") != "rename_excl":
        raise ConfigError(
            "move_primitive 只允许 \"rename_excl\"：不提供退回覆盖式 rename 的配置项"
        )
    categories = cfg.get("categories")
    if categories is not None and tuple(categories) != CATEGORIES:
        raise ConfigError(f"categories 必须恰好是固定的七类：{list(CATEGORIES)}")


def load_settings(
    config_path: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> Settings:
    """加载配置并执行安全闸门检查。缺少配置即拒绝运行。"""
    env = os.environ if env is None else env
    test_root = env.get(TEST_ROOT_ENV)

    if test_root:
        root = _norm_abs(test_root)
        if not os.path.isdir(root):
            raise ConfigError(f"{TEST_ROOT_ENV} 指向的目录不存在：{root}")
        downloads_dir = os.path.join(root, "Downloads")
        state_dir = os.path.join(root, "state")
        cfg: dict[str, Any] = {
            "downloads_dir": downloads_dir,
            "state_dir": state_dir,
            "target_parent_name": DEFAULT_TARGET_PARENT_NAME,
            "categories": list(CATEGORIES),
            "same_filesystem_only": True,
            "on_name_conflict": "skip",
            "move_primitive": "rename_excl",
        }
        _validate_common(cfg)
        target_root = os.path.join(downloads_dir, cfg["target_parent_name"])
        check_protected_paths(downloads_dir, state_dir, target_root)
        check_dir_pair(downloads_dir, state_dir)
        check_target_root_containment(downloads_dir, str(cfg["target_parent_name"]))
        check_not_symlink("/下载目录", downloads_dir)
        check_not_symlink("/目标根目录", target_root)
        return Settings(
            downloads_dir=downloads_dir,
            state_dir=state_dir,
            target_parent_name=cfg["target_parent_name"],
            sandboxed=True,
            source=f"{TEST_ROOT_ENV}={root}",
            raw=cfg,
        )

    path = config_path or os.path.join(PROJECT_DIR, DEFAULT_CONFIG_FILENAME)
    if not os.path.isfile(path):
        raise ConfigError(
            "未找到任何工作路径配置，拒绝运行。\n"
            f"  期望的配置文件：{path}\n"
            f"  或设置测试沙盒变量 {TEST_ROOT_ENV}=<沙盒目录>\n"
            "本工具不会回退到真实 ~/Downloads。"
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"配置文件无法读取或不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(cfg, dict):
        raise ConfigError(f"配置文件顶层必须是 JSON 对象：{path}")

    for key in ("downloads_dir", "state_dir"):
        if not cfg.get(key):
            raise ConfigError(f"配置文件缺少必填字段 {key}：{path}")
    cfg.setdefault("target_parent_name", DEFAULT_TARGET_PARENT_NAME)
    cfg.setdefault("categories", list(CATEGORIES))
    cfg.setdefault("same_filesystem_only", True)
    cfg.setdefault("on_name_conflict", "skip")
    cfg.setdefault("move_primitive", "rename_excl")
    _validate_common(cfg)

    downloads_dir = _norm_abs(str(cfg["downloads_dir"]))
    state_dir = _norm_abs(str(cfg["state_dir"]))
    target_root = os.path.join(downloads_dir, str(cfg["target_parent_name"]))
    check_protected_paths(downloads_dir, state_dir, target_root)
    check_dir_pair(downloads_dir, state_dir)
    check_target_root_containment(downloads_dir, str(cfg["target_parent_name"]))
    check_not_symlink("/下载目录", downloads_dir)
    check_not_symlink("/目标根目录", target_root)
    return Settings(
        downloads_dir=downloads_dir,
        state_dir=state_dir,
        target_parent_name=str(cfg["target_parent_name"]),
        sandboxed=False,
        source=path,
        raw=cfg,
    )


def ensure_state_dirs(settings: Settings) -> None:
    """建立状态目录骨架（0700）。仅写状态目录，绝不写下载目录。"""
    for path in (
        settings.state_dir,
        settings.plans_dir,
        settings.confirmations_dir,
        settings.journals_dir,
        settings.undos_dir,
        settings.plan_state_dir,
    ):
        os.makedirs(path, mode=0o700, exist_ok=True)


def assert_downloads_exists(settings: Settings) -> None:
    if not os.path.isdir(settings.downloads_dir):
        raise ConfigError(f"下载目录不存在：{settings.downloads_dir}")
    if os.path.islink(settings.downloads_dir):
        raise ConfigError(f"下载目录是符号链接，拒绝运行：{settings.downloads_dir}")
