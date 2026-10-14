"""命令行入口：``plan`` / ``execute`` / ``undo`` / ``recover`` / ``status``。

所有命令都通过 :func:`doc_selecter.config.load_settings` 解析工作路径，
**缺少配置或沙盒变量即拒绝运行**，绝不回退到真实 ``~/Downloads``。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__
from .config import (
    ConfigError,
    Settings,
    assert_downloads_exists,
    ensure_state_dirs,
    load_settings,
)
from .executor import (
    ExecutionRefused,
    execute_plan,
    execution_summary,
    recover_execution,
)
from .journal import Journal, JournalCorrupted
from .locking import LockBusy, current_phase, read_plan_state
from .planner import (
    PlanError,
    build_plan,
    drop_confirmation,
    list_plans,
    load_plan,
    load_verified_plan,
    write_confirmation,
    write_plan,
)
from .rules import CATEGORIES, redact_private_text
from .plan_hash import PlanHashError
from .snapshot import read_snapshot
from .undo import UndoRefused, undo_plan, undo_summary

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


def _public(settings: Settings, value: Any) -> str:
    return redact_private_text(str(value), roots=(settings.downloads_dir, settings.target_root,
                                                 settings.state_dir))


def _human_size(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _print_plan_overview(plan: dict[str, Any]) -> None:
    counts = plan.get("counts", {})
    totals = plan.get("category_totals", {})
    print(f"计划编号：{plan['plan_id']}")
    print(f"计划指纹：{plan['plan_hash']}")
    print(f"下载目录：{plan['downloads_dir']}")
    print(f"目标根目录：{plan['target_root']}")
    print(f"规则版本：{plan['rules_version']}")
    print(
        f"纳入分类 {counts.get('included', 0)} 个 · 本次勾选 {counts.get('selected', 0)} 个 · "
        f"排除 {counts.get('excluded', 0)} 个（其中疑似敏感 {counts.get('sensitive_skipped', 0)} 个）"
    )
    print("\n分类总览：")
    sizes = totals.get("sizes", {})
    cat_counts = totals.get("counts", {})
    for category in CATEGORIES:
        mark = "☑" if category in plan["category_selection"] else "☐"
        print(
            f"  {mark} {category:<8} {cat_counts.get(category, 0):>4} 个  "
            f"{_human_size(int(sizes.get(category, 0)))}"
        )
    print("\n将跳过（不计入移动）：")
    excluded = plan.get("excluded", {})
    for label, value in excluded.items():
        if isinstance(value, dict):
            if value.get("names", "missing") is None:
                print(f"  {label}：{value.get('count', 0)} 个（按隐私规则不展示名称）")
            else:
                print(f"  {label}：{value.get('count', 0)} 个")
        else:
            names = list(value)
            extra = "" if len(names) <= 5 else f" 等 {len(names)} 个"
            print(f"  {label}：{len(names)} 个 → {', '.join(names[:5])}{extra}")
    scope = plan.get("scan_scope") or [""]
    nested = [s for s in scope if s]
    print(
        "\n扫描范围："
        + ("仅顶层" if not plan.get("recursive") else f"顶层 + {len(nested)} 个子目录（递归）")
    )
    for rel in nested[:10]:
        print(f"    · {rel}")
    if len(nested) > 10:
        print(f"    …（另有 {len(nested) - 10} 个）")

    print("\n文件明细（原相对路径 → 目标）：")
    for operation in plan.get("operations", []):
        print(
            f"  {operation['op_id']}  [{operation.get('rel_path') or ''}]\n"
            f"          源：{operation['source']}\n"
            f"          目标：{operation['dest']}\n"
            f"          依据：{operation['classification_basis']}  "
            f"（{_human_size(int(operation['identity']['size_bytes']))}，逻辑大小）"
        )


def cmd_plan(settings: Settings, args: argparse.Namespace) -> int:
    assert_downloads_exists(settings)
    selection = args.category or list(CATEGORIES)
    unknown = [c for c in selection if c not in CATEGORIES]
    if unknown:
        print(f"未知类别：{unknown}；可用类别：{list(CATEGORIES)}", file=sys.stderr)
        return EXIT_ERROR
    scope = list(args.include_subfolder or [])
    recursive = bool(scope)
    if args.recursive and not scope:
        print("--recursive 需要配合至少一个 --include-subfolder 使用。", file=sys.stderr)
        return EXIT_ERROR
    build = build_plan(
        settings, category_selection=selection, scope_dirs=scope, recursive=recursive
    )
    plan = build.plan
    path = write_plan(settings, plan)
    _print_plan_overview(plan)
    print(f"\n计划已写入：{path}")
    print(
        "下一步（预览与执行是两步）：\n"
        f"  python3 -m doc_selecter confirm --plan {plan['plan_id']}\n"
        f"  python3 -m doc_selecter execute --plan {plan['plan_id']} "
        f"--confirm {plan['plan_hash']}"
    )
    return EXIT_OK


def cmd_confirm(settings: Settings, args: argparse.Namespace) -> int:
    plan = load_verified_plan(settings, args.plan, for_new_move=True)
    write_confirmation(settings, args.plan, str(plan["plan_hash"]))
    print(f"已确认计划 {args.plan}")
    print(f"确认指纹：{plan['plan_hash']}")
    print(
        "提示：任何方案改动都会使该确认失效，必须重新确认。\n"
        f"执行：python3 -m doc_selecter execute --plan {args.plan} --confirm {plan['plan_hash']}"
    )
    return EXIT_OK


def cmd_execute(settings: Settings, args: argparse.Namespace) -> int:
    assert_downloads_exists(settings)
    print("即将执行移动。执行与撤销互斥，同一时间只能有一个实例操作。")
    try:
        result = execute_plan(settings, args.plan, args.confirm)
    except ExecutionRefused as exc:
        print(f"拒绝执行：{_public(settings, exc)}", file=sys.stderr)
        return EXIT_REFUSED
    print(_public(settings, execution_summary(result)))
    for note in result.recovery_notes:
        print(f"  · {_public(settings, note)}")
    print(
        "\n本次共移动 %d 个文件，可撤销；撤销仅保证把仍未被改动的文件移回原位置，"
        "不保证无条件完整还原。" % result.moved
    )
    print(f"撤销：python3 -m doc_selecter undo --plan {result.plan_id} --confirm")
    return EXIT_OK


def cmd_undo(settings: Settings, args: argparse.Namespace) -> int:
    try:
        result = undo_plan(settings, args.plan, confirm=args.confirm)
    except (UndoRefused, ExecutionRefused) as exc:
        print(f"拒绝撤销：{_public(settings, exc)}", file=sys.stderr)
        return EXIT_REFUSED
    print(_public(settings, undo_summary(result)))
    print("\n撤销仅在条件满足时生效，不保证无条件完整还原。")
    return EXIT_OK


def cmd_recover(settings: Settings, args: argparse.Namespace) -> int:
    try:
        report = recover_execution(settings, args.plan)
    except ExecutionRefused as exc:
        print(f"拒绝恢复：{_public(settings, exc)}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"恢复报告（计划 {report['plan_id']}）：")
    print(f"  阶段：{report['phase']}")
    print(f"  补记已完成：{report.get('recovered') or '无'}")
    print(f"  不可判（需人工）：{report.get('unknown') or '无'}")
    for note in report.get("notes", []):
        print(f"  · {_public(settings, note)}")
    truncated = report.get("truncated") or {}
    if truncated.get("truncated_tail_line"):
        print(f"  末行截断：已封存并开新代次（{_public(settings, truncated.get('notes'))}）")
    print(f"  撤销状态：{report.get('undo_status')}")
    return EXIT_OK


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    if args.plan:
        plan_ids = [args.plan]
    else:
        plan_ids = list_plans(settings)
    if not plan_ids:
        print(f"没有计划。状态目录：{settings.state_dir}")
        return EXIT_OK
    print(f"状态目录：{settings.state_dir}")
    for plan_id in plan_ids:
        try:
            plan = load_plan(settings, plan_id)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[{plan_id}] 计划无法读取：{_public(settings, exc)}")
            continue
        print(f"\n[{plan_id}]")
        print(f"  下载目录：{_public(settings, plan.get('downloads_dir'))}")
        print(f"  目标根目录：{_public(settings, plan.get('target_root'))}")
        print(f"  计划指纹：{plan.get('plan_hash')}")
        print(f"  计划阶段：{current_phase(settings, plan_id)}")
        state = read_plan_state(settings, plan_id)
        if state.get("counts"):
            counts = state["counts"]
            print(
                f"  执行计数：成功 {counts.get('moved', 0)} · 跳过 {counts.get('skipped', 0)} · "
                f"失败 {counts.get('failed', 0)} · 未处理 {counts.get('not_attempted', 0)}"
            )
        snapshot = read_snapshot(settings, plan_id)
        if snapshot:
            print(f"  撤销状态：{snapshot.get('undo_status')}")
            for entry in snapshot.get("entries", []):
                print(
                    f"    {entry['op_id']} {entry['undo_state']:<24} "
                    f"{_public(settings, entry.get('undo_reason') or '')}"
                )
        for kind in ("execution", "undo"):
            journal = Journal(settings.state_dir, plan_id, kind)
            try:
                events, report = journal.read_events()
            except JournalCorrupted as exc:
                print(f"  {kind} 日志：损坏（{_public(settings, exc)}）")
                continue
            print(f"  {kind} 日志：{len(events)} 条事件")
            for note in report.notes:
                print(f"    · {_public(settings, note)}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doc_selecter",
        description="下载文件夹分类助手（预览 → 确认 → 执行 → 撤销）",
        epilog="图形界面：python3 -m doc_selecter.gui",
    )
    parser.add_argument("--version", action="version", version=f"doc-selecter {__version__}")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 <项目>/config.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="只读扫描并生成预览计划（不移动任何文件）")
    p_plan.add_argument("--category", action="append", default=None, help="只勾选指定类别（可重复）")
    p_plan.add_argument(
        "--include-subfolder", action="append", default=None, metavar="相对路径",
        help="递归整理指定的子文件夹（可重复；默认只处理顶层）。选择父目录即包含其所有子目录。",
    )
    p_plan.add_argument(
        "--recursive", action="store_true",
        help="显式声明递归（需与 --include-subfolder 同时使用；仅用于表达意图）",
    )
    p_plan.set_defaults(func=cmd_plan)

    p_confirm = sub.add_parser("confirm", help="确认某一份计划（锁定计划指纹）")
    p_confirm.add_argument("--plan", required=True)
    p_confirm.set_defaults(func=cmd_confirm)

    p_exec = sub.add_parser("execute", help="执行移动（必须携带确认指纹）")
    p_exec.add_argument("--plan", required=True)
    p_exec.add_argument("--confirm", required=True, help="计划指纹")
    p_exec.set_defaults(func=cmd_execute)

    p_undo = sub.add_parser("undo", help="撤销已执行的计划（需显式确认）")
    p_undo.add_argument("--plan", required=True)
    p_undo.add_argument("--confirm", action="store_true", help="确认执行撤销")
    p_undo.set_defaults(func=cmd_undo)

    p_rec = sub.add_parser("recover", help="恢复：封存坏末行、补记可确定结果、重建快照")
    p_rec.add_argument("--plan", required=True)
    p_rec.set_defaults(func=cmd_recover)

    p_status = sub.add_parser("status", help="查看计划、阶段、日志与撤销状态")
    p_status.add_argument("--plan", default=None)
    p_status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    ensure_state_dirs(settings)
    if args.command != "plan":
        print(f"（沙盒模式：{settings.source}）" if settings.sandboxed else f"（配置：{settings.source}）")
    try:
        return int(args.func(settings, args))
    except (PlanError, PlanHashError, LockBusy, ConfigError, ValueError, OSError) as exc:
        print(f"拒绝操作：{_public(settings, exc)}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
