"""Download/hash-check a fixed runtime and prepare an isolated Windows build env."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / 'packaging/runtime-windows-x86_64.json'


def prepare(output, archive=None):
    if sys.platform != 'win32':
        raise RuntimeError('Windows 构建环境必须在 Windows 准备。')
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    if archive is None:
        archive = output / 'runtime.tar.gz'
        urllib.request.urlretrieve(lock['runtime']['url'], archive)
    archive = Path(archive)
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != lock['runtime']['sha256']:
            raise RuntimeError('运行时校验和不符，已停止。')
    runtime = output / 'runtime'
    runtime.mkdir()
    with tarfile.open(archive) as src:
        # Windows archives have no reason to contain a link, drive path or ADS.
        for item in src.getmembers():
            if (not (item.isfile() or item.isdir()) or ':' in item.name or '\\' in item.name or
                    item.name.startswith('/') or '..' in item.name.split('/')):
                raise RuntimeError('运行时归档含不支持的成员。')
        src.extractall(runtime, filter='data')
    python = runtime / 'python/python.exe'
    subprocess.run([str(python), '-I', '-B', '-c',
                    "import sys; assert sys.version.split()[0] == " + repr(lock['python_version'])], check=True)
    venv = output / 'venv'
    subprocess.run([str(python), '-I', '-B', '-m', 'venv', str(venv)], check=True)
    executable = venv / 'Scripts/python.exe'
    backend = output / 'backend-requirements.txt'
    backend.write_text(''.join(line + '\n' for line in
        (REPO / 'packaging/requirements-windows-build.txt').read_text().splitlines()
        if line.startswith('setuptools==')), encoding='utf-8')
    subprocess.run([str(executable), '-I', '-B', '-m', 'pip', '--isolated', 'install',
                    '--no-cache-dir', '--only-binary=:all:', '--require-hashes', '-r', str(backend)], check=True)
    subprocess.run([str(executable), '-I', '-B', '-m', 'pip', '--isolated', 'install',
                    '--no-cache-dir', '--only-binary=:all:', '--no-binary=proxy-tools',
                    '--no-build-isolation', '--require-hashes', '-r',
                    str(REPO / 'packaging/requirements-windows-build.txt')], check=True)
    # No personal path is included in the published manifest; this is local only.
    (output / 'runtime-lock.json').write_text(json.dumps(lock, indent=2), encoding='utf-8')
    print('Windows 隔离构建环境已准备。')
    return executable


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--archive')
    args = parser.parse_args()
    prepare(args.output_dir, args.archive)
