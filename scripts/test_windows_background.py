"""Explicit Windows subset; no whole-suite discovery or GUI initialization."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


def main(receipt):
    if os.name != 'nt':
        raise RuntimeError('此入口必须在 Windows 运行；不能将 macOS 结果冒充原生验证。')
    os.chdir(REPO)
    root = Path(tempfile.mkdtemp(prefix='docsel-windows-'))
    (root / 'tmp').mkdir(); (root / 'records').mkdir()
    os.environ.update(DOCSELECTER_TEST_ROOT=str(root), TEMP=str(root / 'tmp'), TMP=str(root / 'tmp'),
                      PYTHONDONTWRITEBYTECODE='1')
    tempfile.tempdir = str(root / 'tmp')
    sys.path.insert(0, str(REPO))
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        'tests.test_windows_policy', 'tests.windows_native_checks'])
    with (root / 'records/background.log').open('x', encoding='utf-8') as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    spec = importlib.util.spec_from_file_location('windows_builder', REPO / 'scripts/build_windows_distributable.py')
    builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
    summary = {'run': result.testsRun, 'failure': len(result.failures), 'error': len(result.errors),
               'skip': len(result.skipped), 'successful': result.wasSuccessful() and not result.skipped,
               'source_sha256': builder.source_hashes(), 'gui_tested': False,
               'cross_volume_native_tested': False,
               'system': {'build': sys.getwindowsversion().build, 'product_type': sys.getwindowsversion().product_type}}
    path = Path(receipt).absolute(); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(summary, stream, indent=2)
    print({k: summary[k] for k in ('run', 'failure', 'error', 'skip', 'successful')})
    if not summary['successful']:
        # Print test IDs and exception classes only; raw paths/logs remain local.
        for test, trace in result.failures + result.errors:
            print(test.id(), trace.splitlines()[-1].split(':', 1)[0])
        print('原始诊断仅保留在本次人工沙盒 records 中。')
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    raise SystemExit(main(args.receipt))
