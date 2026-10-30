#!/usr/bin/env python3
"""Prepare a pinned runtime/venv in a NEW directory; never install globally."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import urllib.request

REPO = Path(__file__).resolve().parents[1]


def verify_archive(path, expected):
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
        raise ValueError('运行时SHA256不符，拒绝解包或执行。')


def prepare(output, archive=None):
    lock = json.loads((REPO/'packaging/runtime-macos-arm64.json').read_text())
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    (output/'tmp').mkdir()
    if archive is None:
        archive = output/'runtime.tar.gz'
        with urllib.request.urlopen(lock['runtime']['url'],timeout=120) as src, archive.open('xb') as dst:
            while chunk := src.read(1024*1024): dst.write(chunk)
    verify_archive(archive, lock['runtime']['sha256'])
    runtime = output/'runtime'
    with tarfile.open(archive) as src:
        src.extractall(runtime, filter='data')
    python = runtime/'python/bin/python3'
    env = os.environ.copy()
    env.update(TMPDIR=str(output/'tmp'), PYTHONDONTWRITEBYTECODE='1')
    # Bootstrap the already locked build backend before the hash-checked sdist.
    backend = output/'backend-requirements.txt'
    backend.write_text(''.join(line + '\n' for line in (REPO/'packaging/requirements-macos-build.txt').read_text().splitlines() if line.startswith('setuptools==')))
    commands = [[str(python), '-I', '-B', '-m', 'venv', str(output/'venv')],
                [str(output/'venv/bin/python3'), '-I', '-B', '-m', 'pip', '--isolated',
                 '--disable-pip-version-check', 'install', '--no-cache-dir', '--only-binary=:all:',
                 '--require-hashes', '-r', str(backend)],
                [str(output/'venv/bin/python3'), '-I', '-B', '-m', 'pip', '--isolated',
                 '--disable-pip-version-check', 'install', '--no-cache-dir', '--only-binary=:all:',
                 '--no-binary=proxy-tools', '--no-build-isolation', '--require-hashes', '-r', str(REPO/'packaging/requirements-macos-build.txt')]]
    logs = []
    for args in commands:
        r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=300)
        logs.append({'argv':args,'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
        (output/'prepare-results.json').write_text(json.dumps(logs, ensure_ascii=False, indent=2))
        if r.returncode: raise RuntimeError('隔离构建环境准备失败；日志及失败现场已保留。')
    return output/'venv/bin/python3'

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--archive',type=Path,help='Optional previously downloaded archive; SHA256 still required')
    args=parser.parse_args();print(prepare(args.output_dir,args.archive))
