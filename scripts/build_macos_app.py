#!/usr/bin/env python3
"""Build this Mac's local-runtime App, using only stdlib and existing macOS tools."""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import uuid

NAME = '文件整理助手'
DEFAULT_PYTHON = sys.executable
REPO = Path(__file__).resolve().parents[1]
# Explicit whitelist, never copy a tree / config / tests / runtime records.
MODULES = ('__init__.py', '__main__.py', 'app_startup.py', 'atomic_move.py', 'cli.py',
 'config.py', 'directory_archive.py', 'executor.py', 'gui.py', 'gui_remaining.py',
 'gui_session.py', 'gui_source.py', 'journal.py', 'locking.py', 'plan_hash.py',
 'planner.py', 'remaining_archive.py', 'rules.py', 'scanner.py', 'snapshot.py',
 'undo.py', 'watcher.py', 'ui_controller.py', 'webui.py')
WEB_RESOURCES = ('index.html', 'app.css', 'app.js')
ICON_SIZES = ((16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
              (256, 1), (256, 2), (512, 1), (512, 2))


def png_size(path):
    with open(path, 'rb') as f: header = f.read(24)
    if header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
        raise ValueError('图标母版必须为真实PNG')
    return struct.unpack('>II', header[16:24])


def build(output: Path, python: str = DEFAULT_PYTHON, *, repo: Path = REPO) -> Path:
    python = os.path.abspath(python)
    if not os.path.isfile(python) or not os.access(python, os.X_OK):
        raise ValueError('指定的本机Python不存在或不可执行；不会安装依赖。')
    master = repo/'assets/icons/file-organizer-master.png'
    w, h = png_size(master)
    if w != h: raise ValueError('母版不是方形，拒绝裁切或拉伸。')
    for p in [repo/'doc_selecter'/m for m in MODULES] + [repo/'doc_selecter/webui'/n for n in WEB_RESOURCES] + [master, repo/'packaging/bundle_entry.py', repo/'packaging/launcher.m']:
        if p.is_symlink() or not p.is_file(): raise ValueError(f'正式资源缺失或是链接：{p.name}')
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=True)
    app = output/(NAME+'.app')
    app.mkdir()  # Existing product/failed attempt is never replaced or deleted.
    contents = app/'Contents'; resources = contents/'Resources'; macos = contents/'MacOS'
    (resources/'doc_selecter').mkdir(parents=True);macos.mkdir()
    for m in MODULES: shutil.copyfile(repo/'doc_selecter'/m, resources/'doc_selecter'/m)
    (resources/'doc_selecter/webui').mkdir()
    for n in WEB_RESOURCES:
        shutil.copyfile(repo/'doc_selecter/webui'/n, resources/'doc_selecter/webui'/n)
    shutil.copyfile(repo/'packaging/bundle_entry.py',resources/'bundle_entry.py')
    shutil.copyfile(master,resources/'file-organizer-master.png')
    iconset=output/'FileOrganizer.iconset';iconset.mkdir()
    commands=[]
    def run(args):
        r=subprocess.run(args,capture_output=True,text=True,timeout=120)
        commands.append({'argv':args,'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
        (output/'build-tool-results.json').write_text(json.dumps(commands,ensure_ascii=False,indent=2))
        if r.returncode: raise RuntimeError(f'本机构建工具失败：{args[0]}\n{r.stderr}')
    for size, scale in ICON_SIZES:
        name=f'icon_{size}x{size}'+('@2x' if scale==2 else '')+'.png'
        run(['/usr/bin/sips','--resampleHeightWidth',str(size*scale),str(size*scale),str(master),'--out',str(iconset/name)])
        assert png_size(iconset/name)==(size*scale,size*scale)
    run(['/usr/bin/iconutil','--convert','icns',str(iconset),'--output',str(resources/'FileOrganizer.icns')])
    data=(resources/'FileOrganizer.icns').read_bytes()
    if data[:4]!=b'icns' or struct.unpack('>I',data[4:8])[0]!=len(data):raise RuntimeError('图标不是合法icns容器')
    clang='/Library/Developer/CommandLineTools/usr/bin/clang'
    sdk='/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk'
    if not os.path.isfile(clang) or not os.path.isdir(sdk):raise RuntimeError('缺少本机已有的Clang／SDK，未安装。')
    run([clang,'-isysroot',sdk,'-fobjc-arc','-framework','Cocoa',str(repo/'packaging/launcher.m'),'-o',str(macos/'FileOrganizer')])
    os.chmod(macos/'FileOrganizer',0o755)
    info={'CFBundleName':NAME,'CFBundleDisplayName':NAME,'CFBundleIdentifier':'com.yorushikamimimi.docselecter',
          'CFBundleExecutable':'FileOrganizer','CFBundlePackageType':'APPL','CFBundleIconFile':'FileOrganizer.icns',
          'CFBundleVersion':'1','CFBundleShortVersionString':'0.1','NSHighResolutionCapable':True,
          'LSMultipleInstancesProhibited':True,'DocSelecterPython':python,'DocSelecterRuntime':'local Python + Tk; not self-contained'}
    with (contents/'Info.plist').open('wb') as f:plistlib.dump(info,f)
    (contents/'PkgInfo').write_bytes(b'APPL????')
    hashes={str(p.relative_to(resources)):hashlib.sha256(p.read_bytes()).hexdigest() for p in resources.rglob('*') if p.is_file()}
    manifest={'format':1,'name':NAME,'python':python,'source_files':list(MODULES),'resources_sha256':hashes,
              'icon_master_dimensions':[w,h],'icon_conversion':'proportional resample only; original master unchanged',
              'runtime_bundled':False,'developer_id_signed':False,'notarized':False}
    (resources/'build-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--python',default=DEFAULT_PYTHON)
    args=parser.parse_args()
    output=args.output_dir or REPO/'build'/'macos'/(datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:4])
    app=build(output,args.python);print(app)

if __name__=='__main__':main()
