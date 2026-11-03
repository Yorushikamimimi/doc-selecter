"""Build an unsigned onedir ZIP on Windows from an explicit source whitelist."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

REPO = Path(__file__).resolve().parents[1]
CORE = ('__init__', '__main__', 'app_startup', 'atomic_move', 'cli', 'config',
        'directory_archive', 'executor', 'gui', 'gui_remaining', 'gui_session', 'gui_source',
        'journal', 'locking', 'plan_hash', 'planner', 'remaining_archive', 'rules', 'scanner',
        'snapshot', 'undo', 'watcher', 'ui_controller', 'webui')
WINDOWS = ('__init__', 'app', 'io', 'locking', 'move', 'native', 'paths', 'policy', 'smoke')
SOURCES = tuple('doc_selecter/' + n + '.py' for n in CORE) + tuple(
    'doc_selecter/windows/' + n + '.py' for n in WINDOWS) + tuple(
    'doc_selecter/webui/' + n for n in ('index.html', 'app.css', 'app.js')) + (
    'packaging/windows_entry.py', 'packaging/windows_runtime.py')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_hashes():
    return {name: digest(REPO / name) for name in SOURCES}


def validate_environment(lock):
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('必须在 Windows x64 构建。')
    if platform.python_version() != lock['python_version']:
        raise RuntimeError('请使用固定的隔离构建解释器。')
    env_root = Path(sys.prefix).parent
    if (env_root / 'runtime/python').resolve() != Path(sys.base_prefix).resolve():
        raise RuntimeError('构建解释器不来自 prepare_windows_build 的运行时。')
    if json.loads((env_root / 'runtime-lock.json').read_text(encoding='utf-8')) != lock:
        raise RuntimeError('运行时来源锁不一致。')
    for line in (REPO / 'packaging/requirements-windows-build.txt').read_text().splitlines():
        match = re.match(r'([^=]+)==([^ ]+)', line)
        if match and metadata.version(match[1]) != match[2]:
            raise RuntimeError('构建依赖版本不符：' + match[1])


def audit(folder):
    files = []
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('成品不能包含链接。')
        if not path.is_file():
            continue
        relative = path.relative_to(folder).as_posix()
        if path.name in ('config.json', '.env') or any(p in ('tests', 'state', 'records', 'plans', 'journals') for p in path.relative_to(folder).parts):
            raise RuntimeError('成品包含禁止发布的材料。')
        data = path.read_bytes()
        for token in (str(REPO), str(Path.home())):
            for value in (token, token.replace('\\', '/')):
                if value.encode() in data or value.encode('utf-16-le') in data:
                    raise RuntimeError('成品包含本机构建路径。')
        if path.suffix.lower() in ('.exe', '.dll', '.pyd'):
            if data[:2] != b'MZ':
                raise RuntimeError('无效 PE 文件。')
            offset = struct.unpack_from('<I', data, 0x3c)[0]
            machine = struct.unpack_from('<H', data, offset + 4)[0]
            magic = struct.unpack_from('<H', data, offset + 24)[0]
            directories = offset + 24 + (112 if magic == 0x20b else 96)
            managed = bool(struct.unpack_from('<I', data, directories + 14 * 8)[0])
            if machine != 0x8664 and not (machine == 0x14c and managed):
                raise RuntimeError('发现非 x64 的本机可执行组件：' + relative)
        files.append({'path': relative, 'sha256': digest(path), 'size': path.stat().st_size})
    return files


def build(output, receipt):
    lock = json.loads((REPO / 'packaging/runtime-windows-x86_64.json').read_text())
    validate_environment(lock)
    evidence = json.loads(Path(receipt).read_text(encoding='utf-8'))
    hashes = source_hashes()
    if not evidence.get('successful') or evidence.get('source_sha256') != hashes:
        raise RuntimeError('缺少当前源码的 Windows 后台测试通过记录。')
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    stage = output / 'stage'; stage.mkdir()
    for name in SOURCES:
        source = REPO / name
        if source.is_symlink() or not source.is_file():
            raise RuntimeError('白名单源码缺失或为链接。')
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    licenses = stage / 'licenses'; licenses.mkdir()
    shutil.copyfile(REPO / 'LICENSE', licenses / 'LICENSE.doc-selecter.txt')
    shutil.copyfile(Path(sys.base_prefix) / 'LICENSE.txt', licenses / 'LICENSE.Python-runtime.txt')
    tk_license = Path(sys.base_prefix) / 'tcl/tk8.6/license.terms'
    shutil.copyfile(tk_license, licenses / 'LICENSE.Tk.txt')
    for line in (REPO / 'packaging/requirements-windows-build.txt').read_text().splitlines():
        name = line.split('==')[0]
        dist = metadata.distribution(name)
        count = 0
        for item in dist.files or []:
            if any(word in Path(str(item)).name.lower() for word in ('license', 'copying', 'notice')):
                original = Path(dist.locate_file(item))
                if original.is_file() and original.suffix not in ('.py', '.pyc'):
                    count += 1
                    shutil.copyfile(original, licenses / (name + '-' + str(count) + '-' + original.name))
        if not count:
            fallback = REPO / 'packaging/licenses' / ('LICENSE.' + name + '.txt')
            if not fallback.is_file():
                raise RuntimeError('缺少依赖许可：' + name)
            shutil.copyfile(fallback, licenses / fallback.name)
    info = {'version': lock['release_version'], 'runtime': lock, 'source_sha256': hashes,
            'packaging': 'PyInstaller onedir/windowed ZIP', 'signed': False,
            'windows_11_user_acceptance': False}
    (stage / 'build-info.json').write_text(json.dumps(info, indent=2), encoding='utf-8')
    datas = [(str(stage / 'doc_selecter/webui'), 'doc_selecter/webui'), (str(licenses), 'licenses'),
             (str(stage / 'build-info.json'), '.')]
    hidden = ['doc_selecter.' + n for n in CORE if n != '__init__'] + ['doc_selecter.windows.' + n for n in WINDOWS if n != '__init__'] + ['webview.platforms.edgechromium']
    spec = output / 'FileOrganizer.spec'
    spec.write_text(f'''a = Analysis([{str(stage / 'packaging/windows_entry.py')!r}], pathex=[{str(stage)!r}],
    binaries=[], datas={datas!r}, hiddenimports={hidden!r},
    runtime_hooks=[{str(stage / 'packaging/windows_runtime.py')!r}],
    excludes=['tests', 'pip', 'setuptools', 'PyInstaller', 'pytest', 'webview.platforms.mshtml'], optimize=0)
# pywebview ships loaders for multiple CPUs; this distribution is x64 only.
a.binaries = [x for x in a.binaries if not any(v in x[0].replace('\\\\','/') for v in ('win-x86/', 'win-arm64/'))]
a.datas = [x for x in a.datas if not any(v in x[0].replace('\\\\','/') for v in ('win-x86/', 'win-arm64/'))]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='FileOrganizer',
          console=False, debug=False, strip=False, upx=False, uac_admin=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='FileOrganizer')
''', encoding='utf-8')
    env = os.environ.copy()
    env['PYINSTALLER_CONFIG_DIR'] = str(output / 'pyinstaller-cache')
    subprocess.run([sys.executable, '-I', '-X', 'utf8', '-B', '-m', 'PyInstaller', '--noconfirm',
        '--distpath', str(output / 'dist'), '--workpath', str(output / 'work'), str(spec)],
        cwd=stage, env=env, check=True)
    folder = output / 'dist/FileOrganizer'
    artificial = Path(tempfile.mkdtemp(prefix='docsel-frozen-'))
    env['DOCSELECTER_TEST_ROOT'] = str(artificial)
    env['TEMP'] = env['TMP'] = str(artificial)
    subprocess.run([str(folder / 'FileOrganizer.exe'), '--backend-self-test'],
                    cwd=artificial, env=env, timeout=300, check=True)
    frozen_result = json.loads((artificial / 'backend-self-test.json').read_text())
    if frozen_result.get('restored') != 100 or not frozen_result.get('journals_healthy') or not frozen_result.get('frozen'):
        raise RuntimeError('冻结成品后台往返未通过。')
    audit(folder)
    (folder / '使用说明.txt').write_text('Windows 11 x64 / 本地 NTFS 内测版。\n'
        '完整解压后运行 FileOrganizer.exe；无需安装 Python。首次先选择人工文件夹。\n'
        '先预览，确认才移动；同名和跨卷跳过，不覆盖、不改名；可条件撤销。\n'
        '不支持目录归档、网络盘、云同步目录、重解析点、快捷方式、长路径。\n'
        '缺少 WebView2 时提示自行安装，可选择 Tk 备用界面。\n'
        '无签名，系统可能拦截。不要关闭 Defender、SmartScreen 或添加排除项。\n'
        '原生窗口和 Windows 11 用户验收尚未完成。\n', encoding='utf-8')
    manifest = output / 'build-manifest.json'
    manifest.write_text(json.dumps({**info, 'files': audit(folder), 'frozen_backend': frozen_result},
                                   ensure_ascii=False, indent=2), encoding='utf-8')
    archive = output / ('doc-selecter-' + lock['release_version'] + '-windows-x86_64.zip')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as target:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                target.write(path, 'FileOrganizer/' + path.relative_to(folder).as_posix())
    (output / 'SHA256SUMS').write_text(''.join(digest(p) + '  ' + p.name + '\n'
        for p in (archive, manifest)), encoding='utf-8')
    print('Unsigned Windows preview ZIP built; native GUI acceptance remains pending.')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--test-receipt', required=True)
    args = parser.parse_args()
    build(args.output_dir, args.test_receipt)
