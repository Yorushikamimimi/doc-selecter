"""测试沙盒与公用断言。

所有测试都在临时目录中以受控、非敏感名称排他建立沙盒：

    <tmp>/Downloads   模拟下载目录
    <tmp>/state       模拟状态目录

并通过 ``DOCSELECTER_TEST_ROOT`` 注入 —— **绝不触碰真实 ~/Downloads**。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doc_selecter.atomic_move import lstat_identity  # noqa: E402
from doc_selecter.config import TEST_ROOT_ENV, load_settings  # noqa: E402
from doc_selecter.journal import Journal, JournalCorrupted  # noqa: E402
from doc_selecter.planner import (  # noqa: E402
    build_plan,
    load_plan,
    load_verified_plan,
    write_confirmation,
    write_plan,
)
from doc_selecter.rules import CATEGORIES  # noqa: E402
from doc_selecter.snapshot import snapshot_path  # noqa: E402

TARGET_PARENT = "下载分类"


class Sandbox:
    """一个隔离的测试沙盒。"""

    def __init__(self, *, keep: bool = False) -> None:
        # Only fixed words and short hexadecimal groups: random temp suffixes
        # can accidentally contain sensitive directory terms such as "ssh".
        while True:
            token = uuid.uuid4().hex
            name = "docsel-test-" + "-".join(token[i:i + 4] for i in range(0, 32, 4))
            self._tmp = os.path.join(tempfile.gettempdir(), name)
            try:
                os.mkdir(self._tmp, 0o700)
                break
            except FileExistsError:
                continue
        self.root = Path(self._tmp)
        self.downloads = self.root / "Downloads"
        self.state = self.root / "state"
        self.downloads.mkdir(parents=True)
        self.state.mkdir(parents=True)
        self._keep = keep
        self._env_backup: str | None = None
        self._had_env = False

    # --- 生命周期 -----------------------------------------------------------
    def activate(self) -> None:
        self._had_env = TEST_ROOT_ENV in os.environ
        self._env_backup = os.environ.get(TEST_ROOT_ENV)
        os.environ[TEST_ROOT_ENV] = str(self.root)

    def deactivate(self) -> None:
        if self._had_env and self._env_backup is not None:
            os.environ[TEST_ROOT_ENV] = self._env_backup
        else:
            os.environ.pop(TEST_ROOT_ENV, None)

    def close(self) -> None:
        self.deactivate()
        if not self._keep:
            shutil.rmtree(self._tmp, ignore_errors=True)

    @property
    def settings(self):
        return load_settings(env={TEST_ROOT_ENV: str(self.root)})

    # --- 造文件 -------------------------------------------------------------
    def add_file(self, name: str, content: bytes | str = b"payload", *, mtime: float | None = None) -> Path:
        path = self.downloads / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def add_dir(self, name: str) -> Path:
        path = self.downloads / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def add_symlink(self, name: str, target: str) -> Path:
        path = self.downloads / name
        os.symlink(target, path)
        return path

    def set_mtime(self, path: Path, ns: int) -> None:
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, ns))

    def target_dir(self, category: str) -> Path:
        return self.downloads / TARGET_PARENT / category

    def plan_file(self, plan_id: str) -> Path:
        return self.state / "plans" / f"{plan_id}.json"

    # --- 计划 ---------------------------------------------------------------
    def make_plan(
        self,
        *,
        categories: Iterable[str] | None = None,
        confirm: bool = True,
        scope_dirs: Iterable[str] | None = None,
        recursive: bool = False,
    ) -> dict[str, Any]:
        settings = self.settings
        build = build_plan(
            settings,
            category_selection=list(categories) if categories else None,
            scope_dirs=list(scope_dirs) if scope_dirs else None,
            recursive=recursive,
        )
        write_plan(settings, build.plan)
        if confirm:
            write_confirmation(settings, build.plan["plan_id"], str(build.plan["plan_hash"]))
        return build.plan

    def reload_plan(self, plan_id: str) -> dict[str, Any]:
        return load_verified_plan(self.settings, plan_id)

    def journal(self, plan_id: str, kind: str = "execution") -> Journal:
        return Journal(str(self.state), plan_id, kind)


def snapshot_dir(path: Path) -> dict[str, tuple]:
    """目录快照：``{name: (size, mtime_ns, st_ino)}``。"""
    if not path.is_dir():
        return {}
    out: dict[str, tuple] = {}
    with os.scandir(path) as it:
        for entry in it:
            st = entry.stat(follow_symlinks=False)
            out[entry.name] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return out


def tree_snapshot(root: Path) -> dict[str, tuple]:
    """递归快照（用于确认冲突文件未被覆盖）。"""
    out: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        out[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return out


def require_gui_source(app, path: str) -> None:
    """Use the real admission callback; diagnose a refused artificial fixture early."""
    from doc_selecter import gui

    errors = []
    with patch.object(gui.messagebox, 'showerror', side_effect=lambda *a, **k: errors.append(str(a[1]))):
        selected = app.select_source(path)
    if not selected or not app._source_selected or app.settings.downloads_dir != os.path.normpath(path):
        raise AssertionError(
            f"GUI前置失败：来源选择未成功；人工路径={path!r}；返回={selected!r}；"
            f"来源显示={app.source_label['text']!r}；忙碌={app._operation_busy}；"
            f"关闭中={app._closing}；拒绝提示={errors!r}")


def require_gui_preview(app) -> dict:
    """Generate through the product callback, never continue with a missing/stale plan."""
    from doc_selecter import gui

    errors = []
    with patch.object(gui.messagebox, 'showerror', side_effect=lambda *a, **k: errors.append(str(a[1]))):
        app.on_preview()
    if not app._preview_ready or not app.plan or not app.plan.get('plan_id'):
        raise AssertionError(
            f"GUI前置失败：预览未成功生成并保存；人工路径={app.settings.downloads_dir!r}；"
            f"来源已选={app._source_selected}；预览有效={app._preview_ready}；"
            f"状态={app.status_label['text']!r}；问题={app.problem_label['text']!r}；"
            f"错误提示={errors!r}")
    return app.plan


class SandboxTestCase(unittest.TestCase):
    """带沙盒的测试基类。"""

    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.sandbox.activate()
        self.addCleanup(self.sandbox.close)

    # --- 便捷断言 -----------------------------------------------------------
    def assertFileContent(self, path: Path | str, expected: bytes | str) -> None:
        p = Path(path)
        data = p.read_bytes()
        want = expected.encode("utf-8") if isinstance(expected, str) else expected
        self.assertEqual(data, want, f"文件内容不符：{p}")

    def assertExists(self, path: Path | str) -> None:
        self.assertTrue(Path(path).exists() or Path(path).is_symlink(), f"应当存在：{path}")

    def assertNotExists(self, path: Path | str) -> None:
        self.assertFalse(os.path.lexists(str(path)), f"不应当存在：{path}")

    def assertIdentityMatches(self, path: Path | str, identity: dict) -> None:
        actual = lstat_identity(str(path))
        self.assertIsNotNone(actual, f"文件不存在：{path}")
        self.assertTrue(actual.matches(identity), f"身份不匹配：{path}")

    def read_json(self, path: Path | str) -> Any:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)


def assert_journal_valid(test: unittest.TestCase, journal: Journal) -> list[dict]:
    """读取日志并断言无损坏。"""
    try:
        events, _ = journal.read_events()
    except JournalCorrupted as exc:  # pragma: no cover - 失败路径
        test.fail(f"日志被判为损坏：{exc}")
    return events
