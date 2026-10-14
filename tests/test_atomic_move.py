"""原子不覆盖移动原语的**真实**（非打桩）测试。

关键证据：必须在真实文件系统上验证 ``RENAME_EXCL`` 语义 —— 目标存在时返回
``EEXIST``、双方都不变、源仍在。
"""

from __future__ import annotations

import errno
import os
import unittest
from pathlib import Path

from tests.harness import SandboxTestCase

from doc_selecter.atomic_move import (
    AtomicMover,
    AtomicMoveUnavailable,
    DestinationExists,
    SourceMissing,
)


class AtomicMovePrimitiveTests(SandboxTestCase):
    """真实原语（不是打桩）。"""

    def setUp(self) -> None:
        super().setUp()
        self.mover = AtomicMover()
        self.work = self.sandbox.root / "work"
        self.work.mkdir()
        if not self.mover.available:
            self.skipTest(f"环境不提供 RENAME_EXCL：{self.mover.unavailable_reason}")

    def test_atomic_move_exclusive_when_dest_absent(self) -> None:
        src = self.work / "a.bin"
        dst = self.work / "b.bin"
        src.write_bytes(b"NEW")
        before = os.lstat(src)

        self.mover.rename_excl(str(src), str(dst))

        self.assertFalse(src.exists(), "源应当消失")
        self.assertEqual(dst.read_bytes(), b"NEW")
        after = os.lstat(dst)
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino),
                         "目标应当是同一 inode")

    def test_atomic_move_rejects_existing_dest_file(self) -> None:
        src = self.work / "src.bin"
        dst = self.work / "dst.bin"
        src.write_bytes(b"NEW")
        dst.write_bytes(b"OLD")
        os.utime(dst, ns=(111111111, 111111111))
        dst_before = (dst.read_bytes(), os.lstat(dst).st_mtime_ns, os.lstat(dst).st_ino)

        with self.assertRaises(DestinationExists) as ctx:
            self.mover.rename_excl(str(src), str(dst))
        self.assertEqual(ctx.exception.__class__.__name__, "DestinationExists")

        dst_after = (dst.read_bytes(), os.lstat(dst).st_mtime_ns, os.lstat(dst).st_ino)
        self.assertEqual(dst_before, dst_after, "目标内容/mtime/inode 必须逐字节未变")
        self.assertTrue(src.exists(), "源必须仍在")
        self.assertEqual(src.read_bytes(), b"NEW")

    def test_atomic_move_rejects_existing_dest_dir(self) -> None:
        src = self.work / "src2.bin"
        src.write_bytes(b"NEW")
        dst_dir = self.work / "dir"
        dst_dir.mkdir()
        with self.assertRaises(DestinationExists):
            self.mover.rename_excl(str(src), str(dst_dir))
        self.assertTrue(src.exists())
        self.assertTrue(dst_dir.is_dir())

    def test_atomic_move_source_missing_reports_enoent(self) -> None:
        with self.assertRaises(SourceMissing):
            self.mover.rename_excl(str(self.work / "nope.bin"), str(self.work / "x.bin"))

    def test_capability_probe_succeeds_and_cleans_up(self) -> None:
        probe_dir = self.sandbox.state
        before = sorted(os.listdir(probe_dir))
        self.mover.probe_capability(str(probe_dir))
        self.assertEqual(before, sorted(os.listdir(probe_dir)), "探测文件必须被清理")

    def test_capability_probe_does_not_touch_downloads(self) -> None:
        from tests.harness import snapshot_dir

        self.sandbox.add_file("keep.txt", b"untouched")
        before = snapshot_dir(self.sandbox.downloads)
        self.mover.probe_capability(str(self.sandbox.state))
        after = snapshot_dir(self.sandbox.downloads)
        self.assertEqual(before, after, "能力探测不得触碰下载目录")

    def test_rename_excl_at_with_directory_fds(self) -> None:
        src = self.work / "s.bin"
        src.write_bytes(b"FD")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(str(self.work), flags)
        try:
            self.mover.rename_excl_at(fd, "s.bin", fd, "d.bin")
        finally:
            os.close(fd)
        self.assertEqual((self.work / "d.bin").read_bytes(), b"FD")
        self.assertFalse(src.exists())


class AtomicMoveUnavailableTests(SandboxTestCase):
    """原语不可用时必须**拒绝移动**，不得降级。"""

    def test_unavailable_mover_refuses(self) -> None:
        class Broken(AtomicMover):
            def __init__(self) -> None:  # noqa: D401
                self._path_fn = None
                self._at_fn = None
                self._available = False
                self._unavailable_reason = "打桩：模拟原语缺失"

        mover = Broken()
        work = self.sandbox.root / "work2"
        work.mkdir()
        with self.assertRaises(AtomicMoveUnavailable):
            mover.rename_excl(str(work), str(work))
        with self.assertRaises(AtomicMoveUnavailable):
            mover.probe_capability(str(work))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
