"""自动发现新增待整理文件（**纯逻辑**，可在测试中注入时钟）。

设计边界（严格遵守）：

- 只在 GUI 运行期间由界面按 ``after`` 调度调用；本模块**不移动、不确认、不写计划文件**；
- 只在用户选择的范围（顶层/已选子文件夹）内发现，并沿用同一套剔除规则
  （目标分类目录、隐藏项、包目录、代码项目、疑似敏感目录与文件、临时后缀）；
- 「新文件出现」**不等于**「下载完成」：必须排除临时后缀，并要求**连续观察
  大小与修改时间稳定**超过 ``settle_seconds``，才报告为「新增待整理」；
  否则报告为「尚未稳定」。这只是**启发式**判断，不保证下载已完成；
- 时钟通过 ``clock`` 注入，测试无需真实等待。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .atomic_move import FileIdentity
from .rules import is_temp_download
from .scanner import collect_files, ScanIssue

#: 两次观察之间至少间隔多久才计入「稳定观察」（与稳定时长分开控制）
DEFAULT_SETTLE_SECONDS = 5.0
#: 默认定时检查间隔（界面用）
DEFAULT_INTERVAL_SECONDS = 30.0
#: 单次最多报告多少条，避免界面被刷屏
MAX_REPORTED = 50


@dataclass
class TrackedFile:
    rel_path: str
    size_bytes: int
    mtime_ns: int
    first_seen: float
    last_seen: float
    observations: int = 1
    stable_since: float | None = None

    def signature(self) -> tuple[int, int]:
        return (self.size_bytes, self.mtime_ns)


@dataclass
class DiscoveryReport:
    new_files: list[str] = field(default_factory=list)        # 稳定 → 新增待整理
    settling: list[str] = field(default_factory=list)         # 尚未稳定
    temp_skipped: list[str] = field(default_factory=list)     # 临时后缀
    total_seen: int = 0
    checked_at: float = 0.0
    error: str | None = None
    scan_issues: list[ScanIssue] = field(default_factory=list)
    partial_coverage: bool = False
    total_new: int = 0
    total_settling: int = 0
    total_temp: int = 0

    @property
    def has_updates(self) -> bool:
        return bool(self.new_files or self.settling or self.temp_skipped)


class NewFileWatcher:
    """定时发现新增文件（纯内存状态，不写任何文件）。"""

    def __init__(
        self,
        *,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settle_seconds = float(settle_seconds)
        self.interval_seconds = float(interval_seconds)
        self._clock = clock
        self._tracked: dict[str, TrackedFile] = {}
        self._reported: set[str] = set()
        self._disabled = True
        self.last_report: DiscoveryReport | None = None

    # --- 时钟（测试可显式覆盖，避免真实等待） --------------------------------
    def set_clock(self, clock: Callable[[], float]) -> None:
        self._clock = clock

    def set_settle_seconds(self, seconds: float) -> None:
        self.settle_seconds = float(seconds)

    # --- 开关 ---------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return not self._disabled

    def enable(self) -> None:
        self._disabled = False

    def disable(self) -> None:
        """关闭检查：清空观察状态，避免恢复后误报旧文件为「新增」。"""
        self._disabled = True
        self.reset()

    def reset(self, *, keep_reported: bool = False) -> None:
        """清空跟踪状态（切换范围/刷新预览后调用）。

        ``keep_reported=True`` 时保留「已经提示过」的集合，用于刷新预览后
        避免重复提示同一批文件。
        """
        self._tracked.clear()
        if not keep_reported:
            self._reported.clear()
        self.last_report = None

    def mark_known(self, rel_paths: Iterable[str]) -> None:
        """把已在计划/已整理集合中的文件标记为已知（不重复发现）。"""
        self._reported.update(rel_paths)

    # --- 单次检查 -----------------------------------------------------------
    def check_once(
        self,
        *,
        downloads_dir: str,
        target_parent_name: str,
        scope_dirs=None,
        recursive: bool = False,
        extra_ignored: Iterable[str] | None = None,
        cancel_event=None,
    ) -> DiscoveryReport:
        """做一次发现（只读扫描 + 内存比较），**绝不移动或写盘**。"""
        now = self._clock()
        report = DiscoveryReport(checked_at=now)
        if not self.enabled:
            self.last_report = report
            return report
        try:
            files: dict[str, FileIdentity] = collect_files(
                downloads_dir,
                target_parent_name=target_parent_name,
                scope_dirs=scope_dirs,
                recursive=recursive,
                cancel_event=cancel_event,
            )
        except (OSError, ValueError) as exc:
            report.error = f"读取失败：{type(exc).__name__}"
            self.last_report = report
            return report

        # 同一遍安全扫描同时返回普通文件、临时项与结构化失败。
        report.scan_issues = list(getattr(files, "scan_issues", []))
        report.partial_coverage = bool(report.scan_issues)
        if report.partial_coverage:
            report.error = f"检查覆盖不完整：{len(report.scan_issues)} 处读取失败/扫描变化"
        temps = sorted(getattr(files, "temp_files", []))
        report.total_temp = len(temps)
        report.temp_skipped = temps[:MAX_REPORTED]

        ignored = set(extra_ignored or ())
        seen: set[str] = set()
        for rel_path, identity in sorted(files.items()):
            if identity is None:
                continue
            seen.add(rel_path)
            if rel_path in ignored or rel_path in self._reported:
                continue

            tracked = self._tracked.get(rel_path)
            signature = (identity.size_bytes, identity.mtime_ns)
            if tracked is None:
                # 首次观察到：开始稳定计时（此时还不能判定「已下载完成」）
                self._tracked[rel_path] = TrackedFile(
                    rel_path=rel_path,
                    size_bytes=identity.size_bytes,
                    mtime_ns=identity.mtime_ns,
                    first_seen=now,
                    last_seen=now,
                    stable_since=now,
                )
                report.settling.append(rel_path)
                continue

            if tracked.signature() != signature:
                # 大小或修改时间变了 → 仍在写入，稳定计时从头开始
                tracked.size_bytes, tracked.mtime_ns = signature
                tracked.last_seen = now
                tracked.observations += 1
                tracked.stable_since = now
                report.settling.append(rel_path)
                continue

            tracked.last_seen = now
            tracked.observations += 1
            if tracked.stable_since is None:
                tracked.stable_since = now
            if now - tracked.stable_since >= self.settle_seconds:
                report.new_files.append(rel_path)
            else:
                report.settling.append(rel_path)

        # 已在跟踪但本轮没看到的文件（被移走/改名）：移除跟踪
        for gone in [p for p in self._tracked if p not in seen and not report.partial_coverage]:
            self._tracked.pop(gone, None)

        report.total_seen = len(seen)
        report.total_new = len(report.new_files)
        report.total_settling = len(report.settling)
        report.new_files = report.new_files[:MAX_REPORTED]
        self._reported.update(report.new_files)
        report.settling = report.settling[:MAX_REPORTED]
        report.temp_skipped = report.temp_skipped[:MAX_REPORTED]
        self.last_report = report
        return report

    # --- 提示文案 -----------------------------------------------------------
    @staticmethod
    def format_report(report: DiscoveryReport) -> str:
        lines = ["自动检查结果（仅提示，不会自动移动或确认；点击「生成预览」更新明细）"]
        if report.error:
            lines.append(f"  {report.error}（仅显示已读取部分，未做任何移动）")
        lines.append(
            f"  新增待整理 {report.total_new} · 尚未稳定 {report.total_settling} · "
            f"疑似临时文件 {report.total_temp} · 已读取 {report.total_seen} 个文件"
        )
        if report.new_files:
            lines.extend(f"    + {p}" for p in report.new_files[:MAX_REPORTED])
            if report.total_new > len(report.new_files):
                lines.append(f"    …（还有 {report.total_new - len(report.new_files)} 个，下次继续提示，也可直接生成预览）")
        if report.settling:
            lines.append("  尚未稳定（可能仍在下载/写入；不保证下载已完成）：")
            lines.extend(f"    · {p}" for p in report.settling[:10])
        if report.temp_skipped:
            lines.append("  疑似临时/未完成下载（已排除）：")
            lines.extend(f"    × {p}" for p in report.temp_skipped[:10])
        if not report.has_updates and not report.error:
            lines.append("  未发现新的待整理文件。")
        return "\n".join(lines)
