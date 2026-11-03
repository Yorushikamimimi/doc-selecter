"""Explicit, background-only frozen-backend check, using freshly created fake data."""
import json
import os
from pathlib import Path
import sys
import tempfile


def run():
    raw = os.environ.get('DOCSELECTER_TEST_ROOT')
    if not raw:
        raise RuntimeError('后台自检必须明确注入人工沙盒。')
    from .native import open_directory
    with open_directory(raw):
        pass
    root = Path(tempfile.mkdtemp(prefix='wincheck-', dir=raw))
    source, state = root / 'Downloads', root / 'state'
    source.mkdir(); state.mkdir()
    from ..config import load_settings, ensure_state_dirs
    from ..planner import build_plan, write_plan, write_confirmation
    from ..executor import execute_plan, classify_journals
    from ..undo import undo_plan
    from ..atomic_move import lstat_identity
    settings = load_settings(env={'DOCSELECTER_TEST_ROOT': str(root)})
    payloads = {}
    extensions = ('pdf', 'jpg', 'zip', 'mp3', 'exe', 'csv', 'odd')
    for i in range(100):
        name = ('资料/' if i % 2 else '') + f'样例_{i:03d}.' + extensions[i % len(extensions)]
        target = source / name
        target.parent.mkdir(exist_ok=True)
        payloads[name] = ('ARTIFICIAL-' + name).encode('utf-8')
        target.write_bytes(payloads[name])
    before = {name: lstat_identity(str(source / name)).as_dict() for name in payloads}
    ensure_state_dirs(settings)
    plan = build_plan(settings, recursive=True, scope_dirs=['资料']).plan
    if len(plan['operations']) != 100:
        raise AssertionError('预览未包含全部 100 个人工文件。')
    for name, value in before.items():
        if lstat_identity(str(source / name)).as_dict() != value:
            raise AssertionError('只读预览改变了来源。')
    if Path(settings.target_root).exists():
        raise AssertionError('预览创建了目标目录。')
    write_plan(settings, plan)
    write_confirmation(settings, plan['plan_id'], plan['plan_hash'])
    moved = execute_plan(settings, plan['plan_id'], plan['plan_hash'])
    if moved.moved != 100 or moved.failed or moved.skipped:
        raise AssertionError('100 文件整理未全部完成。')
    restored = undo_plan(settings, plan['plan_id'], confirm=True)
    if restored.restored != 100 or restored.undo_status != 'completed':
        raise AssertionError('100 文件撤销未全部完成。')
    for name, data in payloads.items():
        path = source / name
        if path.read_bytes() != data or lstat_identity(str(path)).as_dict() != before[name]:
            raise AssertionError('人工文件撤销后字节或身份不符。')
    for op in plan['operations']:
        if Path(op['dest']).exists():
            raise AssertionError('撤销后目标仍存在。')
    if not all(item['state'].healthy for item in classify_journals(settings, plan['plan_id']).values()):
        raise AssertionError('日志不健康。')
    frozen = bool(getattr(sys, 'frozen', False))
    if frozen:
        import _tkinter
        bundle = Path(sys._MEIPASS).resolve()
        for name, module in sys.modules.items():
            if name.startswith('doc_selecter') and getattr(module, '__file__', None):
                if not Path(module.__file__).resolve().is_relative_to(bundle):
                    raise AssertionError('冻结成品加载了外部源码。')
        if not Path(_tkinter.__file__).resolve().is_relative_to(bundle):
            raise AssertionError('冻结成品加载了外部 Tk 扩展。')
    return {'files': 100, 'moved': moved.moved, 'restored': restored.restored,
            'journals_healthy': True, 'gui_tested': False, 'frozen': frozen}


def main():
    raw = os.environ.get('DOCSELECTER_TEST_ROOT')
    if not raw:
        return 64
    result = run()
    # Caller creates a fresh root; never overwrite an old receipt.
    with (Path(raw) / 'backend-self-test.json').open('x', encoding='utf-8') as stream:
        json.dump(result, stream)
    return 0
