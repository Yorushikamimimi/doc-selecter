#!/usr/bin/env python3
"""Build an arm64 self-contained App/DMG with the pinned, isolated PyInstaller env."""
from __future__ import annotations
import datetime
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('local_app_builder', REPO/'scripts/build_macos_app.py')
_local = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_local)
MODULES = _local.MODULES
NAME = _local.NAME
LOCK_FILE = REPO/'packaging/runtime-macos-arm64.json'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_environment(lock):
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise RuntimeError('本构建入口只交付已选择的macOS arm64产物。')
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('请在隔离构建虚拟环境运行，不修改全局Python。')
    if platform.python_version() != lock['python_version']:
        raise RuntimeError('Python版本与固定运行时不符。')
    for line in (REPO/'packaging/requirements-macos-build.txt').read_text().splitlines():
        name, version = line.split()[0].split('==')
        if importlib.metadata.version(name) != version:
            raise RuntimeError('构建依赖版本不符：' + name)


def release_notes(lock):
    return f'''# 文件整理助手 v{lock['release_version']}公开预览版

- macOS Apple Silicon / arm64，自带Python {lock['python_version']}、Tcl/Tk，无需另装Python。
- 功能沿用公开源码基线：先选来源、预览并确认整理，按既有身份和日志契约撤销。
- 构建最低系统声明macOS {lock['deployment_target']}；实际仅在macOS 27.0（26A428）arm64测试，其他系统及另一台干净Mac未验证。
- 本地ad-hoc签名，无Developer ID或公证。网络下载后Gatekeeper可能拦截，未验证下载隔离体验；不要关闭全局安全保护或自动移除隔离属性。
- 不自动安装、替换已有应用或读取用户下载目录。启动后来源未选，用户须主动选择。
- 计划、确认和日志保留在原应用支持位置，不能当缓存删除。外部变动可能阻止完整撤销。
- 用户原生交互及剩余项归档专项原生人工验收尚未完成，程序化验证不等于人工验收。
- 请从官方公开仓库Release核对版本与SHA256SUMS：https://github.com/Yorushikamimimi/doc-selecter/releases
'''


def installation_guide(lock):
    return (
        f'文件整理助手 {lock["release_version"]} 公开预览版 · Apple Silicon arm64\n\n'
        '拖入Applications是手动安装；若已有同名应用，请先核对版本，不要未经确认替换。\n'
        '自带Python／Tcl／Tk，首次启动须选择来源，不自动扫描。\n'
        f'构建最低系统声明macOS {lock["deployment_target"]}；实际仅测试macOS 27.0（26A428）arm64；其他系统及另一台干净Mac未验证。\n'
        '本地ad-hoc签名，无Developer ID、未公证；网络下载后的Gatekeeper体验未验证，macOS可能拦截，勿关闭全局保护或自动移除隔离属性。\n'
        '用户原生交互及剩余项归档专项原生人工验收尚未完成。\n'
        '请核对官方Release、版本与SHA256SUMS：https://github.com/Yorushikamimimi/doc-selecter/releases\n')


def build(output: Path, *, make_dmg=True) -> Path:
    lock = json.loads(LOCK_FILE.read_text())
    validate_environment(lock)
    sources = [REPO/'doc_selecter'/m for m in MODULES]
    sources += [REPO/'packaging'/n for n in ('frozen_entry.py', 'frozen_runtime.py')]
    sources += [REPO/'packaging/licenses'/n for n in lock['license_files']]
    sources += [REPO/'assets/icons/file-organizer-master.png', REPO/'LICENSE']
    for p in sources:
        if p.is_symlink() or not p.is_file():
            raise ValueError('正式白名单资源缺失或为链接：' + p.name)
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=False)  # Retain existing/failed products.
    stage = output/'stage'; (stage/'doc_selecter').mkdir(parents=True)
    commands = []

    def run(args, timeout=300):
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        commands.append({'argv':args, 'returncode':r.returncode, 'stdout':r.stdout, 'stderr':r.stderr})
        (output/'build-tool-results.json').write_text(json.dumps(commands, ensure_ascii=False, indent=2))
        if r.returncode:
            raise RuntimeError('构建工具失败（已保留日志）：' + args[0] + '\n' + r.stderr[-2000:])
        return r

    for m in MODULES:
        shutil.copyfile(REPO/'doc_selecter'/m, stage/'doc_selecter'/m)
    for n in ('frozen_entry.py', 'frozen_runtime.py'):
        shutil.copyfile(REPO/'packaging'/n, stage/n)
    licenses = stage/'licenses'; licenses.mkdir()
    for n in lock['license_files']:
        shutil.copyfile(REPO/'packaging/licenses'/n, licenses/n)
    shutil.copyfile(REPO/'LICENSE', licenses/'LICENSE.doc-selecter.txt')
    shutil.copyfile(REPO/'packaging/THIRD_PARTY.md', licenses/'THIRD_PARTY.md')
    master = REPO/'assets/icons/file-organizer-master.png'
    if _local.png_size(master) != (1254, 1254):
        raise ValueError('正式母版尺寸不符；拒绝重绘、裁切或拉伸。')
    iconset = output/'FileOrganizer.iconset'; iconset.mkdir()
    for size, scale in _local.ICON_SIZES:
        n = f'icon_{size}x{size}' + ('@2x' if scale == 2 else '') + '.png'
        run(['/usr/bin/sips', '--resampleHeightWidth', str(size*scale), str(size*scale), str(master), '--out', str(iconset/n)])
        assert _local.png_size(iconset/n) == (size*scale, size*scale)
    icon = stage/'FileOrganizer.icns'
    run(['/usr/bin/iconutil', '--convert', 'icns', str(iconset), '--output', str(icon)])
    source_hashes = {m:digest(stage/'doc_selecter'/m) for m in MODULES}
    metadata = {'format':1, 'version':lock['release_version'], 'runtime':lock,
                'source_sha256':source_hashes, 'icon_master_sha256':digest(master),
                'packaging':'PyInstaller onedir/windowed', 'developer_id':False, 'notarized':False}
    (stage/'build-info.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    datas = [(str(licenses), 'licenses'), (str(stage/'build-info.json'), '.'), (str(icon), '.')]
    spec = output/'FileOrganizer.spec'
    info = {'CFBundleDisplayName':NAME, 'CFBundleName':NAME,
            'CFBundleShortVersionString':lock['release_version'], 'CFBundleVersion':lock['release_version'],
            'LSMinimumSystemVersion':lock['deployment_target'], 'NSHighResolutionCapable':True,
            'LSMultipleInstancesProhibited':True, 'DocSelecterRuntime':'bundled Python + Tcl/Tk'}
    spec.write_text(f'''a = Analysis([{str(stage/'frozen_entry.py')!r}], pathex=[{str(stage)!r}],
    binaries=[], datas={datas!r}, hiddenimports={['doc_selecter.'+Path(m).stem for m in MODULES if m!='__init__.py']!r},
    runtime_hooks=[{str(stage/'frozen_runtime.py')!r}],
    excludes=['tests','pip','setuptools','PyInstaller','pytest'], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='FileOrganizer',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, argv_emulation=False, target_arch='arm64', codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='FileOrganizer')
app = BUNDLE(coll, name={NAME+'.app'!r}, icon={str(icon)!r},
    bundle_identifier='com.yorushikamimimi.docselecter', info_plist={info!r})
''')
    env = os.environ.copy()
    env['PYINSTALLER_CONFIG_DIR'] = str(output/'pyinstaller-cache')
    env['MACOSX_DEPLOYMENT_TARGET'] = lock['deployment_target']
    # No user site or cwd imports; analysis only sees the explicit staged package.
    args = [sys.executable, '-I', '-B', '-m', 'PyInstaller', '--distpath', str(output/'dist'),
            '--workpath', str(output/'work'), str(spec)]
    r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=600)
    commands.append({'argv':args, 'returncode':r.returncode, 'stdout':r.stdout, 'stderr':r.stderr})
    (output/'build-tool-results.json').write_text(json.dumps(commands, ensure_ascii=False, indent=2))
    if r.returncode:
        raise RuntimeError('PyInstaller失败（保留完整日志）：\n' + r.stderr[-3000:])
    app = output/'dist'/(NAME+'.app')
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)])
    # App is sealed by PyInstaller. Never append manifests or data after signing.
    manifest = dict(metadata)
    manifest['app_files'] = {}
    for p in sorted(app.rglob('*')):
        n = str(p.relative_to(app))
        if p.is_symlink():
            target = os.readlink(p)
            if not p.resolve().is_relative_to(app.resolve()) or not p.exists():
                raise ValueError('App包含越界／断裂链接：' + n)
            manifest['app_files'][n] = {'link':target}
        elif p.is_file():
            manifest['app_files'][n] = {'sha256':digest(p), 'mode':oct(p.stat().st_mode & 0o777)}
    (output/'build-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    notes = output/'release-notes.md'; notes.write_text(release_notes(lock))
    if make_dmg:
        image_stage = output/'image-stage'; image_stage.mkdir()
        shutil.copytree(app, image_stage/app.name, symlinks=True)
        (image_stage/'Applications').symlink_to('/Applications', target_is_directory=True)
        (image_stage/'使用说明.txt').write_text(installation_guide(lock))
        dmg = output/f'doc-selecter-{lock["release_version"]}-macos-arm64.dmg'
        run(['/usr/bin/hdiutil', 'create', '-srcfolder', str(image_stage), '-volname', NAME,
             '-format', 'UDZO', '-imagekey', 'zlib-level=9', str(dmg)], timeout=300)
        run(['/usr/bin/hdiutil', 'verify', str(dmg)], timeout=120)
        summed = [dmg, output/'build-manifest.json', notes]
        (output/'SHA256SUMS').write_text(''.join(digest(p)+'  '+p.name+'\n' for p in summed))
    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--app-only', action='store_true')
    args = parser.parse_args()
    output = args.output_dir or REPO/'build'/'release'/(datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:4])
    print(build(output, make_dmg=not args.app_only))

if __name__ == '__main__':
    main()
