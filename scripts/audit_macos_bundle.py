"""Read-only self-contained bundle audit. Never resolve user source/config paths."""
from pathlib import Path
import hashlib
import json
import os
import plistlib
import re
import subprocess

MACH_MAGIC = {b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}
SYSTEM = ('/usr/lib/', '/System/Library/')


def resolve_dependency(name, binary, app, rpaths):
    if name.startswith(SYSTEM):
        return 'system'
    roots = []
    if name.startswith('@loader_path/'):
        roots = [binary.parent/name[len('@loader_path/'):]]
    elif name.startswith('@executable_path/'):
        roots = [app/'Contents/MacOS'/name[len('@executable_path/'):]]
    elif name.startswith('@rpath/'):
        suffix = name[len('@rpath/'):]
        for r in rpaths:
            if r == '@loader_path': roots.append(binary.parent/suffix)
            elif r.startswith('@loader_path/'):
                roots.append(binary.parent/r[len('@loader_path/'):]/suffix)
            elif r == '@executable_path': roots.append(app/'Contents/MacOS'/suffix)
            elif r.startswith('@executable_path/'):
                roots.append(app/'Contents/MacOS'/r[len('@executable_path/'):]/suffix)
    for p in roots:
        if p.resolve().is_relative_to(app.resolve()) and p.exists():
            return str(p.resolve().relative_to(app.resolve()))
    raise ValueError('越界或未解析的运行依赖：' + name)


def audit(app: Path):
    app = app.absolute()
    info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
    if info.get('DocSelecterPython'):
        raise ValueError('分发App仍绑定外部Python。')
    files, mach, links, paths = {}, {}, {}, []
    forbidden = {'config.json','.env','.git','tests','fixtures','inventory.json','plan_state','confirmations'}
    for p in sorted(app.rglob('*')):
        n = str(p.relative_to(app))
        if any(part.lower() in forbidden for part in p.relative_to(app).parts):
            raise ValueError('App含非正式材料：' + n)
        if p.is_symlink():
            if not p.resolve().is_relative_to(app.resolve()) or not p.exists():
                raise ValueError('越界或断裂的bundle链接：' + n)
            links[n] = os.readlink(p); continue
        if not p.is_file(): continue
        data = p.read_bytes();mode = p.stat().st_mode & 0o777
        if mode & 0o022: raise ValueError('资源可被组／其他用户写入：' + n)
        files[n] = {'sha256':hashlib.sha256(data).hexdigest(), 'mode':oct(mode), 'size':len(data)}
        # Literal paths in debug metadata are audited separately from load commands.
        hits = re.findall(rb'/Users/[^\x00\r\n\t "<>]{1,180}', data)
        if hits: paths.append({'file':n, 'literal_user_paths':sorted(set(x.decode('utf-8',errors='replace') for x in hits))})
        if data[:4] not in MACH_MAGIC: continue
        kind = subprocess.check_output(['/usr/bin/file','-b',str(p)],text=True).strip()
        if 'arm64' not in kind or 'x86_64' in kind: raise ValueError('不是单一arm64：' + n)
        load = subprocess.check_output(['/usr/bin/otool','-l',str(p)],text=True)
        deps = subprocess.check_output(['/usr/bin/otool','-L',str(p)],text=True).splitlines()[1:]
        rpaths = re.findall(r'cmd LC_RPATH\n\s*cmdsize \d+\n\s*path (.*?) \(offset',load)
        for r in rpaths:
            if not r.startswith(('@loader_path','@executable_path')):
                raise ValueError('rpath不是bundle相对路径：' + n + ': ' + r)
        minimum = re.findall(r'\bminos ([\d.]+)',load) or re.findall(r'cmd LC_VERSION_MIN_MACOSX\n\s*cmdsize \d+\n\s*version ([\d.]+)',load)
        identifiers = re.findall(r'cmd LC_ID_DYLIB\n\s*cmdsize \d+\n\s*name (.*?) \(offset', load)
        resolved = {}
        for line in deps:
            name = line.strip().split(' (compatibility')[0]
            if name and name not in identifiers:
                resolved[name] = resolve_dependency(name,p,app,rpaths)
        mach[n] = {'kind':kind,'rpaths':rpaths,'install_identifiers':identifiers,'minimum_system':minimum,'dependencies':resolved}
    declared = tuple((list(map(int, info['LSMinimumSystemVersion'].split('.'))) + [0, 0])[:3])
    for n,m in mach.items():
        for v in m['minimum_system']:
            if tuple((list(map(int, v.split('.'))) + [0, 0])[:3]) > declared:
                raise ValueError('Mach-O最低系统高于Info声明：' + n + ': ' + v)
    if not mach or not any('libpython' in n for n in mach) or not any('libtcl' in n for n in mach):
        raise ValueError('缺少内置Python／Tcl／Tk动态库。')
    return {'plist':info,'files':files,'links':links,'mach_o':mach,'user_path_literals':paths,
            'architecture':'arm64','declared_minimum_system':info['LSMinimumSystemVersion'],
            'no_external_runtime_load_dependencies':True}

if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('app',type=Path)
    args=parser.parse_args();print(json.dumps(audit(args.app),ensure_ascii=False,indent=2))
