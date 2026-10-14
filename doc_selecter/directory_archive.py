"""Schema v3 directory/layout evidence. Original identities never change.

The position projection is derived from the immutable plan and the two validated
journals. It is never derived from a snapshot or an inode search.
"""
from __future__ import annotations
import errno
import hashlib
import os
import re
import stat
import unicodedata
from .atomic_move import (AtomicMover, DestinationExists, DirectoryIdentityChanged,
                         MoveFailed, OutOfScopeError, lstat_identity, assert_no_symlink_segments)
from .journal import Journal, JournalCorrupted, JournalWriteFailed, make_event
from .plan_hash import V3_HASH_ALG
from .rules import (CATEGORIES, document_subcategory, normalize_extension,
                    is_sensitive_relative_path, is_code_project_dir, classify_entry)

LAYOUT_VERSION = 'empty-dir-slots-v1'
RISK_NOTICE = '请暂停下载和其他写入；检查与移动之间新增的内容仍可能随目录移动，发现异常将停止并保留记录。'
FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)


def nfc(text):
    return unicodedata.normalize('NFC', text)


def chains(rel):
    return [''] + ['/'.join(rel.split('/')[:i]) for i in range(1, len(rel.split('/'))+1)] if rel else ['']


def _error(message):
    from .planner import PlanError
    raise PlanError(message)


def identity(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        _error('目录不存在、链接或类型不符：'+path)
    return [info.st_dev, info.st_ino]


def directory_matches(path, expected):
    return identity(path) == [expected['st_dev'], expected['st_ino']]


def open_checked(path, expected=None):
    """Open each raw component without following links, then compare final fd."""
    fd = os.open('/', FLAGS)
    try:
        for part in path.split('/')[1:]:
            new_fd = os.open(part, FLAGS, dir_fd=fd)
            os.close(fd); fd = new_fd
        info = os.fstat(fd)
        if expected is not None and [info.st_dev, info.st_ino] != list(expected):
            raise DirectoryIdentityChanged('目录 fd 身份变化：'+path)
        return fd
    except BaseException:
        os.close(fd); raise


def names(path):
    fd = open_checked(path)
    try:
        values = os.listdir(fd)
        if len(set(map(nfc, values))) != len(values):
            _error('目录成员 NFC 歧义')
        return sorted(map(nfc, values))
    finally:
        os.close(fd)


def raw_guard(root, path):
    try:
        assert_no_symlink_segments(root,path)
    except OutOfScopeError as exc:
        _error(str(exc))


def prepare_archive_plan(settings, plan, scan, enabled):
    if type(enabled) is not bool:
        _error('归档开关必须是布尔值')
    plan['plan_hash_alg'] = V3_HASH_ALG
    batch = plan['plan_id']
    plan['archive'] = {'enabled': enabled, 'batch_id': batch,
                       'archive_layout_version': LAYOUT_VERSION,
                       'relative_root': f'{settings.target_parent_name}/已归档/{batch}/空文件夹'}
    candidates = {}
    file_by_rel = {nfc(op['rel_path']): op for op in plan['operations']}
    for rel in sorted((nfc(v) for v in scan.scan_scope if v), key=lambda v: (-v.count('/'), v)) if enabled else []:
        if not any(s and (rel == nfc(s) or rel.startswith(nfc(s)+'/')) for s in scan.selected_scope):
            continue
        if is_sensitive_relative_path(rel) or rel.split('/')[0] == settings.target_parent_name:
            continue
        path = plan['downloads_dir']+'/'+rel
        members = names(path)
        if not members or is_code_project_dir(frozenset(members))[0]:
            continue
        direct = []
        for name in members:
            op = file_by_rel.get(rel+'/'+name) or candidates.get(rel+'/'+name)
            if op is None:
                break
            direct.append({'name': name, 'op_id': op['op_id']})
        else:
            ident = identity(path)
            from .planner import normalized_dir_identity_map
            recorded=normalized_dir_identity_map(plan['dir_identities']['source_dirs'],'source_dirs').get(rel)
            if recorded is not None and recorded != ident: _error('预览目录身份变化')
            if ident is None: _error('预览期间目录消失')
            op_id = f'dir-{len(candidates)+1:04d}'
            candidates[rel] = {'op_id': op_id, 'kind': 'empty_directory', 'rel_path': rel,
                               'parent_rel_path': rel.rpartition('/')[0], 'source': path,
                               'dest': plan['downloads_dir']+'/'+plan['archive']['relative_root']+'/'+op_id+'/'+rel.rsplit('/',1)[-1],
                               'identity': {'st_dev': ident[0], 'st_ino': ident[1], 'mode_type': 'dir'},
                               'preview_nonempty': True, 'enumeration_complete': True,
                               'direct_members': direct, 'depends_on': [v['op_id'] for v in direct]}
    plan['directory_operations'] = list(candidates.values())
    di = plan['dir_identities']
    di['source_dirs'].setdefault('', di['downloads_dir'])
    for rel, op in candidates.items():
        for parent in chains(rel):
            di['source_dirs'].setdefault(parent, identity(plan['downloads_dir']+('/'+parent if parent else '')))
    di['archive_dirs'] = {}
    for op in candidates.values():
        rel = os.path.relpath(os.path.dirname(op['dest']), plan['target_root']).replace(os.sep,'/')
        for parent in chains(rel)[1:]:
            path = plan['target_root']+'/'+parent
            raw_guard(plan['downloads_dir'], path)
            di['archive_dirs'].setdefault(parent, identity(path))
    # Reject raw destination links before any path canonicalization can erase them.
    for op in plan['operations']:
        raw_guard(plan['downloads_dir'], os.path.dirname(op['dest']))
    validate_structure(settings, plan)


def layout_operations(plan):
    di = plan['dir_identities']
    values = {'': di['target_root']}
    for field in ('dest_dirs','archive_dirs'):
        for rel, expected in di[field].items():
            rel=nfc(rel)
            if rel in values and values[rel] != expected: _error('目标父链身份歧义')
            values[rel] = expected
    return { 'layout-'+hashlib.sha256(rel.encode()).hexdigest()[:20]:
             {'op_id': 'layout-'+hashlib.sha256(rel.encode()).hexdigest()[:20],
              'kind': 'layout_directory', 'rel_path': rel, 'dest': plan['target_root']+('/'+rel if rel else ''),
              'expected': expected}
             for rel, expected in sorted(values.items(),key=lambda v:(v[0].count('/'),v[0])) }


def validate_structure(settings, plan):
    if plan.get('schema_version') == 4:
        from .remaining_archive import validate_structure as remaining_structure
        return remaining_structure(settings, plan)
    from . import RULES_VERSION
    from .planner import normalized_dir_identity_map
    from .scanner import _classify_dir
    if plan.get('schema_version') != 3 or plan.get('rules_version') != RULES_VERSION or plan.get('plan_hash_alg') != V3_HASH_ALG:
        _error('v3 规则／布局／哈希版本不兼容')
    if plan.get('downloads_dir') != settings.canonical_downloads_dir or plan.get('target_root') != settings.canonical_target_root:
        _error('计划根与可信配置不一致')
    a = plan.get('archive')
    if not isinstance(a,dict) or set(a) != {'enabled','batch_id','archive_layout_version','relative_root'} or type(a['enabled']) is not bool:
        _error('归档授权字段缺失或非法')
    if a['archive_layout_version'] != LAYOUT_VERSION or not isinstance(a['batch_id'],str) or not re.fullmatch(r'[a-z0-9-]+',a['batch_id']):
        _error('归档布局或批次非法')
    if a['relative_root'] != f"{settings.target_parent_name}/已归档/{a['batch_id']}/空文件夹":
        _error('归档固定根被改写')
    def rel_ok(rel, empty=False):
        if not isinstance(rel,str) or (not rel and not empty) or '\x00' in rel or (rel and any(p in ('','.','..') for p in rel.split('/'))):
            _error('相对路径非法')
        return nfc(rel)
    scopes = plan.get('selected_scope')
    actual = plan.get('scan_scope')
    if not isinstance(scopes,list) or not isinstance(actual,list) or '' not in scopes or '' not in actual or type(plan.get('recursive')) is not bool:
        _error('范围字段非法')
    for s in scopes+actual:
        rel_ok(s,True)
        if s and (any(not _classify_dir(p)[0] for p in s.split('/')) or s.split('/')[0]==settings.target_parent_name): _error('范围包含受保护目录')
    if not plan['recursive'] and (scopes != [''] or actual != ['']): _error('递归范围不一致')
    def in_scope(rel):
        return bool(plan['recursive'] and any(s and (rel == nfc(s) or rel.startswith(nfc(s)+'/')) for s in scopes))
    for s in actual:
        if s and not in_scope(nfc(s)): _error('实际范围超出明确授权')
    if plan.get('category_selection') != [c for c in CATEGORIES if c in plan.get('category_selection',[])]: _error('类别非法')
    files = plan.get('operations'); dirs = plan.get('directory_operations')
    if not isinstance(files,list) or not isinstance(dirs,list) or (not a['enabled'] and dirs): _error('目录候选授权不一致')
    all_ops = files+dirs
    if any(not isinstance(v,dict) or not isinstance(v.get('op_id'),str) for v in all_ops): _error('操作 ID 非法')
    if len({v['op_id'] for v in all_ops}) != len(all_ops) or len({nfc(v.get('rel_path','')) for v in all_ops}) != len(all_ops): _error('操作重复／歧义')
    expected_source = {''}
    expected_dest = set()
    for op in files:
        if op.get('kind') != 'file' or not re.fullmatch(r'op-[0-9]{4,}',op['op_id']): _error('文件操作 kind／ID 非法')
        rel = rel_ok(op.get('rel_path')); parent,_,name = rel.rpartition('/')
        if parent and not in_scope(parent): _error('文件不在明确扫描范围')
        category = op.get('category'); sub = op.get('document_subcategory')
        classification=classify_entry(name,stat.S_IFREG)
        if classification.status != 'included' or classification.category != category or classification.basis != op.get('classification_basis'):
            _error('文件类别／筛查／依据与固定规则不一致')
        if category not in plan['category_selection']: _error('文件类别不在授权集合')
        expected_sub = document_subcategory(normalize_extension(name)) if category == '文档资料' and not parent else None
        if expected_sub != sub or (expected_sub and not sub): _error('当前规则顶层文档缺少有效子类绑定')
        dest_rel = category+'/'+(sub+'/' if sub else '')+rel
        if op.get('source') != plan['downloads_dir']+'/'+rel or op.get('dest') != plan['target_root']+'/'+dest_rel:
            _error('固定文件目标被改写；不允许链接替代去向')
        expected_source.update(chains(parent))
        expected_dest.update(chains(dest_rel.rpartition('/')[0])[1:])
    expected_archive = set()
    dir_inodes = set()
    for op in dirs:
        rel = rel_ok(op.get('rel_path')); parent,_,name = rel.rpartition('/')
        if (op.get('kind') != 'empty_directory' or not re.fullmatch(r'dir-[0-9]{4,}',op['op_id'])
            or not in_scope(rel) or rel not in map(nfc,actual) or is_sensitive_relative_path(rel)
            or rel.split('/')[0] == settings.target_parent_name): _error('目录不在归档授权范围')
        if op.get('parent_rel_path') != parent or op.get('source') != plan['downloads_dir']+'/'+rel:
            _error('目录原相对路径不一致')
        dest = plan['downloads_dir']+'/'+a['relative_root']+'/'+op['op_id']+'/'+name
        if op.get('dest') != dest: _error('固定目录槽位被改写')
        ident = op.get('identity')
        if not isinstance(ident,dict) or set(ident) != {'st_dev','st_ino','mode_type'} or ident['mode_type'] != 'dir' or any(type(ident[k]) is not int for k in ('st_dev','st_ino')):
            _error('目录身份字段非法')
        pair = (ident['st_dev'],ident['st_ino'])
        if pair in dir_inodes: _error('目录对象重复')
        dir_inodes.add(pair)
        if op.get('preview_nonempty') is not True or op.get('enumeration_complete') is not True: _error('目录预览非空／完整性缺失')
        direct = sorted([{'name': nfc(v['rel_path']).rsplit('/',1)[-1], 'op_id': v['op_id']} for v in all_ops if nfc(v['rel_path']).rpartition('/')[0] == rel],key=lambda v:v['name'])
        if not direct or op.get('direct_members') != direct or op.get('depends_on') != [v['op_id'] for v in direct]: _error('目录直接成员或依赖被改写')
        expected_source.update(chains(rel))
        expected_archive.update(chains(os.path.relpath(os.path.dirname(dest),plan['target_root']).replace(os.sep,'/'))[1:])
    # A finite tree of direct dependencies, ending in admitted file operations.
    def has_file(op,seen):
        if op['op_id'] in seen: _error('目录依赖循环')
        if op in files: return True
        deps=[v for v in all_ops if v['op_id'] in op['depends_on']]
        return bool(deps) and all(has_file(v,seen|{op['op_id']}) for v in deps)
    if any(not has_file(op,set()) for op in dirs): _error('目录缺少本批文件因果依赖')
    di = plan.get('dir_identities')
    if not isinstance(di,dict) or set(di) != {'downloads_dir','target_root','source_dirs','dest_dirs','archive_dirs'}: _error('父链身份字段缺失')
    for field, keys in (('source_dirs',expected_source),('dest_dirs',expected_dest),('archive_dirs',expected_archive)):
        values=normalized_dir_identity_map(di.get(field) or {},field)
        if set(values) != keys: _error('计划缺少父链身份或含额外路径：'+field)
        for rel,value in values.items():
            if value is not None and (not isinstance(value,list) or len(value)!=2 or any(type(v) is not int for v in value)): _error('父链身份非法')
            if field=='source_dirs' and value is None: _error('原目录身份不可为空')
    for op in dirs:
        if normalized_dir_identity_map(di['source_dirs'],'source_dirs').get(op['rel_path']) != [op['identity']['st_dev'],op['identity']['st_ino']]: _error('候选与父链身份不一致')
    if di['downloads_dir'] != di['source_dirs'].get(''): _error('来源根身份不一致')


def read_folds(settings, plan):
    folds=[]
    events=[]
    for kind in ('execution','undo'):
        try:
            ev=Journal(settings.state_dir,plan['plan_id'],kind).read_valid_prefix()
        except JournalCorrupted as exc:
            raise JournalCorrupted(('撤销' if kind=='undo' else '执行')+'日志损坏：'+str(exc)) from exc
        events.append(ev)
        folded={}
        for e in ev: folded.setdefault(e['op_id'],{})[e['event']]=e
        folds.append(folded)
    validate_evidence(plan,events[0],events[1])
    return folds


def validate_evidence(plan, executions, undos):
    operations={v['op_id']:v for v in plan['operations']+plan['directory_operations']}
    layouts=layout_operations(plan)
    moved=set(); restored=set(); created=set()
    for ev in executions+undos:
        op=operations.get(ev['op_id']) or layouts.get(ev['op_id'])
        if op is None or ev.get('op_kind') != op.get('kind','file') or ev.get('plan_hash') != plan['plan_hash'] or ev.get('journal_schema_version') != (3 if plan['schema_version'] == 4 else 2) or ev.get('plan_schema_version') != plan['schema_version'] or ev.get('log_kind') != ('undo' if ev['event'].startswith('undo_') else 'execution'):
            raise JournalCorrupted('v3 事件缺少合法版本／操作／计划绑定')
        event=ev['event']
        kind=op.get('kind','file')
        if event=='result':
            allowed={'layout_directory':{'created','not_created'},'empty_directory':{'archived','skipped','failed','unknown_needs_review'},'file':{'moved','skipped','failed'}}[kind]
            if ev.get('outcome') not in allowed or not isinstance(ev.get('reason_code'),str): raise JournalCorrupted('result payload 或 outcome 非法')
        if event=='intent' and kind!='layout_directory':
            parent=os.path.dirname(op['dest'])
            for key,layout in layouts.items():
                if (parent==layout['dest'] or parent.startswith(layout['dest']+'/')) and layout['expected'] is None and key not in created:
                    raise JournalCorrupted('用户对象 intent 前目标容器尚未持久化绑定')
        if op.get('kind') == 'layout_directory':
            if event not in ('intent','result') or op['expected'] is not None: raise JournalCorrupted('非法 layout 事件')
            if event=='intent' and (ev.get('dest') != op['dest'] or ev.get('identity') is not None): raise JournalCorrupted('layout intent 被改写')
            if event=='result' and ev.get('outcome')=='created':
                if not isinstance(ev.get('dest_identity'),list) or len(ev['dest_identity'])!=2 or any(type(v) is not int for v in ev['dest_identity']): raise JournalCorrupted('layout 身份缺失')
                created.add(op['op_id'])
        elif event=='intent':
            if ev.get('source') != op['source'] or ev.get('dest') != op['dest'] or ev.get('identity') != op['identity']: raise JournalCorrupted('intent 路径／原身份被改写')
            if kind=='empty_directory' and (type(ev.get('attempted')) is not bool or ev.get('dependencies') != op['depends_on']): raise JournalCorrupted('目录 intent 缺少 attempted／依赖绑定')
            if op.get('kind')=='empty_directory' and ev.get('attempted',True):
                if not all(v in moved for v in op['depends_on']) or ev.get('dependencies') != op['depends_on']: raise JournalCorrupted('归档依赖无在先成功结果')
        elif event=='result':
            if ev.get('outcome') in ('moved','archived'):
                prior=next(v for v in executions if v['op_id']==op['op_id'] and v['event']=='intent')
                if kind=='empty_directory' and not prior.get('attempted'): raise JournalCorrupted('未尝试目录不能记录成功')
                if ev.get('dest_identity') != op['identity']: raise JournalCorrupted('result 改写身份基准')
                moved.add(op['op_id'])
        elif event in ('undo_intent','undo_skip'):
            if op['op_id'] not in moved: raise JournalCorrupted('未成功移动的操作不能获取撤销授权')
            if op.get('kind') != 'empty_directory' and any(v['op_id'] in moved and v['op_id'] not in restored for v in plan['directory_operations']): raise JournalCorrupted('目录屏障未完成，不允许文件撤销')
            if event=='undo_skip':
                state = ev.get('undo_state')
                code = ev.get('reason_code')
                if not isinstance(ev.get('reason'), str) or not isinstance(code, str):
                    raise JournalCorrupted('undo_skip 缺少合法原因字段')
                if state not in {
                    'blocked_source_occupied', 'blocked_dest_changed',
                    'blocked_dest_missing', 'blocked_dir_changed',
                    'unknown_needs_review',
                }:
                    raise JournalCorrupted('undo_skip 状态非法，不能伪造 restored')
                expected_codes = {
                    'blocked_source_occupied': {'DEST_EXISTS'},
                    'blocked_dest_changed': {'DEST_CHANGED'},
                    'blocked_dest_missing': {'DEST_MISSING'},
                    # 全局撤销屏障可能先以 UNKNOWN_STATE 持久化；目录单项
                    # 结果仍必须使用 DIR_IDENTITY_CHANGED。
                    'blocked_dir_changed': {'DIR_IDENTITY_CHANGED', 'UNKNOWN_STATE'},
                    'unknown_needs_review': {
                        'UNKNOWN_STATE', 'ATOMIC_MOVE_UNAVAILABLE', 'PERMISSION_DENIED',
                    },
                }
                if code not in expected_codes[state]:
                    raise JournalCorrupted('undo_skip 状态与原因码不一致')
                continue
            if ev.get('from') != op['dest'] or ev.get('to') != op['source'] or ev.get('expected_identity') != op['identity']: raise JournalCorrupted('undo intent 被改写')
            if op.get('kind')=='empty_directory':
                parent=next((v for v in plan['directory_operations'] if v['rel_path']==op['parent_rel_path']),None)
                if parent and parent['op_id'] in moved and parent['op_id'] not in restored: raise JournalCorrupted('目录还原顺序非法')
        elif event=='undo_result':
            outcome = ev.get('outcome')
            state = ev.get('undo_state')
            code = ev.get('reason_code')
            if op['op_id'] not in moved:
                raise JournalCorrupted('未成功移动的操作不能记录撤销结果')
            if not isinstance(ev.get('reason'), str) or not isinstance(code, str):
                raise JournalCorrupted('undo_result 缺少合法原因字段')
            if outcome == 'restored':
                if state != 'restored' or code != 'OK':
                    raise JournalCorrupted('undo_result restored 的状态或原因码不一致')
                restored.add(op['op_id'])
            elif outcome == 'skipped':
                expected_codes = {
                    'blocked_source_occupied': 'DEST_EXISTS',
                    'blocked_dest_changed': 'DEST_CHANGED',
                    'blocked_dest_missing': 'DEST_MISSING',
                }
                if state not in expected_codes or code != expected_codes[state]:
                    raise JournalCorrupted('undo_result skipped 状态与原因码不一致')
            elif outcome == 'failed':
                expected_codes = {
                    'blocked_dir_changed': {'DIR_IDENTITY_CHANGED'},
                    'unknown_needs_review': {
                        'UNKNOWN_STATE', 'RESTORED_IDENTITY_MISMATCH',
                        'ATOMIC_MOVE_UNAVAILABLE', 'PERMISSION_DENIED',
                        'CROSS_FILESYSTEM', 'LOG_WRITE_FAILED',
                    },
                }
                if state not in expected_codes or code not in expected_codes[state]:
                    raise JournalCorrupted('undo_result failed 状态与原因码不一致')
            else:
                raise JournalCorrupted('undo_result outcome 非法，不能伪造撤销完成')


def verify_binding(settings, plan):
    """Pure admission: both logs read first, then all original and fixed-slot chains."""
    validate_structure(settings,plan)
    execution,undos=read_folds(settings,plan)
    if plan.get('schema_version') == 4:
        from .remaining_archive import live_admission
        live_admission(settings, plan, execution)
    from .planner import normalized_dir_identity_map
    di=dict(plan['dir_identities'])
    for field in ('source_dirs','dest_dirs','archive_dirs'):
        di[field]=normalized_dir_identity_map(di[field],field)
    if identity(plan['downloads_dir']) != di['downloads_dir']: _error('来源根身份变化')
    runtime={}
    for key,op in layout_operations(plan).items():
        raw_guard(plan['downloads_dir'],op['dest'])
        slot=execution.get(key,{})
        result=slot.get('result') or {}
        expected=op['expected']
        if expected is None and result.get('outcome')=='created': expected=result['dest_identity']
        actual=identity(op['dest'])
        if expected is not None and actual != expected: _error('目标父目录身份发生变化（身份与计划不一致）：'+op['rel_path'])
        if expected is None and actual is not None: _error('未持久化绑定的目标容器：'+op['rel_path'])
        runtime[op['dest']]=expected
    dir_by_rel={v['rel_path']:v for v in plan['directory_operations']}
    projection={}
    for rel,expected in di['source_dirs'].items():
        path=plan['downloads_dir']+('/'+rel if rel else '')
        raw_guard(plan['downloads_dir'],path)
        op=dir_by_rel.get(rel)
        location=path
        if op:
            slot=execution.get(op['op_id'],{}); un=undos.get(op['op_id'],{})
            result=slot.get('result') or {}; ur=un.get('undo_result') or {}
            if result.get('outcome')=='unknown_needs_review': _error('目录已发生移动、待检查')
            raw_guard(plan['downloads_dir'],op['dest'])
            archived=result.get('outcome')=='archived'
            pending=slot.get('intent') and not slot.get('result')
            src=identity(path)
            dst=identity(op['dest']) if archived or (pending and src is None and slot['intent'].get('attempted',True)) or un else None
            if un.get('undo_intent') and not ur:
                if src==expected and dst is None: location=path
                elif src is None and dst==expected: location=op['dest']
                else: _error('目录撤销中断位置不可判')
            elif ur.get('outcome')=='restored':
                if src!=expected or dst is not None: _error('已还原原目录身份／位置变化')
            elif archived or pending:
                if src is None and dst==expected: location=op['dest']
                elif pending and src==expected and dst is None: location=path
                else: _error('归档原位置冲突或目录身份变化')
            if location==op['dest'] and names(location): _error('归档目录出现未知内容，待检查')
            if location==path and archived:
                allowed=[]
                for member in op['direct_members']:
                    u=undos.get(member['op_id'],{})
                    member_op=next(v for v in plan['operations']+plan['directory_operations'] if v['op_id']==member['op_id'])
                    finished=(u.get('undo_result') or {}).get('outcome')=='restored'
                    pending_member=u.get('undo_intent') and not u.get('undo_result')
                    if finished or pending_member:
                        observed=lstat_identity(member_op['source'])
                        expected_member=member_op['identity']
                        matched= observed is not None and (directory_matches(member_op['source'],expected_member) if member_op.get('kind')=='empty_directory' else observed.matches(expected_member))
                        if pending_member and observed is None:
                            destination=lstat_identity(member_op['dest'])
                            still_in_slot=destination is not None and (directory_matches(member_op['dest'],expected_member) if member_op.get('kind')=='empty_directory' else destination.matches(expected_member))
                            if not still_in_slot: _error('撤销成员位置不可判')
                            continue
                        if not matched or os.path.lexists(member_op['dest']): _error('已还原成员身份／位置不可判')
                        allowed.append(member['name'])
                if names(path)!=sorted(allowed): _error('已还原原目录出现未知成员，待检查')
        if identity(location)!=expected: _error('原父目录身份与计划不一致或未知缺失：'+rel)
        fd=open_checked(location,expected);os.close(fd)
        projection[rel]=location
    if not any(v.get('intent') for key,v in execution.items() if not key.startswith('layout-')):
        for op in plan['directory_operations']:
            members=names(op['source'])
            if is_code_project_dir(frozenset(members))[0]: _error('目录项目保护，拒绝归档')
            if members != [v['name'] for v in op['direct_members']]: _error('目录直接成员与预览授权不一致，请重新预览')
    for op in plan['operations']:
        # The complete fixed target chain is always checked before parsing links.
        raw_guard(plan['downloads_dir'],op['dest'])
        raw_guard(plan['downloads_dir'],op['source'])
    return execution,undos,runtime,projection


def ensure_layout(settings,plan,hook=None):
    journal=Journal(settings.state_dir,plan['plan_id'],'execution')
    _,_,runtime,_=verify_binding(settings,plan)
    for key,op in layout_operations(plan).items():
        if runtime[op['dest']] is not None: continue
        parent=os.path.dirname(op['dest'])
        parent_expected=runtime.get(parent,plan['dir_identities']['downloads_dir'] if parent==plan['downloads_dir'] else None)
        fd=open_checked(parent,parent_expected)
        try:
            journal.append(make_event('intent',key,dest=op['dest'],identity=None))
            if hook: hook('before_layout',op)
            verify_binding(settings,plan)
            os.mkdir(os.path.basename(op['dest']),dir_fd=fd)
            child=open_checked(op['dest'])
            current=list((os.fstat(child).st_dev,os.fstat(child).st_ino))
            try:
                if hook: hook('after_layout',op)
                if identity(op['dest']) != current: _error('容器创建后身份变化，未绑定当前观测')
                os.fsync(child);os.fsync(fd)
            finally: os.close(child)
            journal.append(make_event('result',key,outcome='created',dest_identity=current,reason_code='OK'))
            runtime[op['dest']]=current
        finally: os.close(fd)
    return runtime


def _dir_event(journal,op,event,**extra):
    journal.append(make_event(event,op['op_id'],**extra))


def archive_directories(settings,plan,result,hook=None):
    journal=Journal(settings.state_dir,plan['plan_id'],'execution')
    for op in sorted(plan['directory_operations'],key=lambda v:(-v['rel_path'].count('/'),v['rel_path'])):
        if result.stopped:
            result.dirs_not_attempted+=1; continue
        execution,_,runtime,_=verify_binding(settings,plan)
        deps=all((execution.get(d,{}).get('result') or {}).get('outcome') in ('moved','archived') for d in op['depends_on'])
        outcome='skipped';code='DEPENDENCY_NOT_MOVED';reason='本批依赖未全部成功';scope='local'
        attempted=False; moved=False;srcfd=dstfd=objfd=None
        try:
            if deps:
                for dep in op['depends_on']:
                    dep_op=next(v for v in plan['operations']+plan['directory_operations'] if v['op_id']==dep)
                    actual=lstat_identity(dep_op['dest'])
                    if dep_op.get('kind')=='empty_directory':
                        if not directory_matches(dep_op['dest'],dep_op['identity']): _error('依赖目录身份变化')
                    elif actual is None or not actual.matches(dep_op['identity']): _error('依赖文件身份变化')
                objfd=open_checked(op['source'],[op['identity']['st_dev'],op['identity']['st_ino']])
                if os.listdir(objfd):
                    code='NOT_EMPTY';reason='仍有内容（隐藏项也计入），未归档'
                else:
                    attempted=True
                    srcfd=open_checked(os.path.dirname(op['source']),plan['dir_identities']['source_dirs'][op['parent_rel_path']])
                    dstfd=open_checked(os.path.dirname(op['dest']),runtime[os.path.dirname(op['dest'])])
            if attempted:
                os.fsync(objfd);os.fsync(srcfd);os.fsync(dstfd)
            _dir_event(journal,op,'intent',source=op['source'],dest=op['dest'],identity=op['identity'],attempted=attempted,dependencies=op['depends_on'])
            if attempted:
                if os.listdir(objfd):
                    code='NOT_EMPTY';reason='移动前复查发现新增内容'
                else:
                    if os.fstat(srcfd).st_dev != os.fstat(dstfd).st_dev: raise MoveFailed(errno.EXDEV,'跨卷拒绝')
                    if hook: hook('before_directory_move',op)
                    # Revalidate full parents after callbacks; injection of content remains allowed by controlled boundary.
                    verify_binding(settings,plan)
                    AtomicMover().rename_excl_at(srcfd,os.path.basename(op['source']),dstfd,os.path.basename(op['dest']))
                    moved=True
                    if hook: hook('after_directory_move',op)
                    raw_guard(plan['downloads_dir'],op['dest'])
                    if (os.path.lexists(op['source']) or not directory_matches(op['dest'],op['identity']) or os.listdir(objfd)):
                        outcome='unknown_needs_review';code='DIRECTORY_MOVED_NEEDS_REVIEW';reason='已发生移动、待检查：后检内容或身份异常';scope='global'
                    else:
                        os.fsync(objfd);os.fsync(srcfd);os.fsync(dstfd)
                        outcome='archived';code='OK';reason='已归档原空目录'
            if hook: hook('before_directory_result',op)
            _dir_event(journal,op,'result',outcome=outcome,reason_code=code,reason=reason,scope=scope,
                       dest_identity=op['identity'] if outcome=='archived' else None)
        except (JournalWriteFailed,JournalCorrupted): raise
        except Exception as exc:
            if isinstance(exc,DestinationExists) and not moved: code='DEST_EXISTS';scope='local'
            elif isinstance(exc,MoveFailed) and exc.errno==errno.EXDEV and not moved: code='CROSS_FILESYSTEM';scope='local'
            else: scope='global';outcome='unknown_needs_review' if moved else 'failed';code='DIRECTORY_MOVED_NEEDS_REVIEW' if moved else 'DIR_IDENTITY_CHANGED'
            reason=str(exc)
            fold,_=journal.fold()
            if fold.get(op['op_id'],{}).get('intent') and not fold[op['op_id']].get('result'):
                _dir_event(journal,op,'result',outcome=outcome,reason_code=code,reason=reason,scope=scope)
            else:
                _dir_event(journal,op,'intent',source=op['source'],dest=op['dest'],identity=op['identity'],attempted=False,dependencies=op['depends_on'])
                _dir_event(journal,op,'result',outcome=outcome,reason_code=code,reason=reason,scope=scope)
        finally:
            for fd in (srcfd,dstfd,objfd):
                if fd is not None: os.close(fd)
        result.directory_outcomes.append({'op_id':op['op_id'],'outcome':outcome,'reason_code':code,'reason':reason})
        if outcome=='archived': result.archived_dirs+=1
        elif outcome=='skipped': result.dirs_skipped+=1
        else: result.dirs_failed+=1
        if scope=='global': result.stopped=True;result.stop_reason=reason


def directory_snapshot(plan,execution,undos):
    entries=[]
    for op in plan['directory_operations']:
        slot=execution.get(op['op_id'],{});ur=undos.get(op['op_id'],{})
        res=slot.get('result') or {}
        state='skipped_not_moved'
        if res.get('outcome')=='archived': state='pending'
        if slot.get('intent') and (not res or res.get('outcome')=='unknown_needs_review'): state='unknown_needs_review'
        if ur.get('undo_intent'): state='attempting'
        if ur.get('undo_result'): state=ur['undo_result'].get('undo_state','unknown_needs_review')
        if ur.get('undo_skip'): state=ur['undo_skip'].get('undo_state','unknown_needs_review')
        entries.append({'op_id':op['op_id'],'original_source':op['source'],'current_dest':op['dest'],
                        'dest_identity':op['identity'],'undo_state':state,'undo_reason':res.get('reason','')})
    return entries


def recover_v3(settings,plan,write=True):
    from .snapshot import derive_snapshot,write_snapshot
    from .locking import write_plan_state,current_phase
    execution,undos,_,_=verify_binding(settings,plan)
    repair_notes=[Journal(settings.state_dir,plan['plan_id'],kind).classify().detail for kind in ('execution','undo') if Journal(settings.state_dir,plan['plan_id'],kind).classify().repairable]
    proposed=[];unknown=[]
    for key,op in layout_operations(plan).items():
        slot=execution.get(key,{})
        if slot.get('intent') and not slot.get('result'):
            if identity(op['dest']) is None:
                proposed.append(('execution',make_event('result',key,outcome='not_created',reason_code='NOT_CREATED',reason='容器未创建，不自动重试')))
            else: unknown.append(key)
    all_ops=plan['operations']+plan['directory_operations']
    for op in all_ops:
        slot=execution.get(op['op_id'],{})
        if not slot.get('intent') or slot.get('result'): continue
        source=lstat_identity(op['source']);dest=lstat_identity(op['dest'])
        is_dir=op.get('kind')=='empty_directory'
        matches=lambda x: x is not None and (x.as_dict()['st_dev']==op['identity']['st_dev'] and x.as_dict()['st_ino']==op['identity']['st_ino'] and x.mode_type=='dir' if is_dir else x.matches(op['identity']))
        if source is None and matches(dest) and (not is_dir or not names(op['dest'])):
            proposed.append(('execution',make_event('result',op['op_id'],outcome='archived' if is_dir else 'moved',reason_code='RECOVERED_FROM_INTENT',reason='从持久化 intent 补记，未移动',dest_identity=op['identity'])))
        elif matches(source) and dest is None:
            proposed.append(('execution',make_event('result',op['op_id'],outcome='skipped',reason_code='NOT_MOVED',reason='源仍在，未重试移动')))
        else: unknown.append(op['op_id'])
    for op in plan['directory_operations']:
        slot=undos.get(op['op_id'],{})
        if slot.get('undo_intent') and not slot.get('undo_result'):
            src=identity(op['source']);dst=identity(op['dest']);expected=[op['identity']['st_dev'],op['identity']['st_ino']]
            if src==expected and dst is None:
                proposed.append(('undo',make_event('undo_result',op['op_id'],outcome='restored',reason_code='OK',undo_state='restored',reason='补记原目录已还原')))
            elif src is None and dst==expected: pass
            else: unknown.append(op['op_id'])
    for op in plan['operations']:
        result=execution.get(op['op_id'],{}).get('result') or {}
        if result.get('reason_code') in ('MOVE_IDENTITY_MISMATCH','UNKNOWN_ERROR'): unknown.append(op['op_id'])
    if unknown:
        return {'plan_id':plan['plan_id'],'phase':'needs_review','unknown':unknown,'recovered':[],'notes':['位置不可判，未补写或替换快照'],'undo_status':'needs_review'}
    if write:
        for kind in ('execution','undo'): Journal(settings.state_dir,plan['plan_id'],kind).repair_if_needed()
        for kind,event in proposed: Journal(settings.state_dir,plan['plan_id'],kind).append(event)
        execution,undos,_,_=verify_binding(settings,plan)
        snap=derive_snapshot(plan=plan,execution_fold=execution,undo_fold=undos)
        write_snapshot(settings,plan['plan_id'],snap)
    else:
        snap=derive_snapshot(plan=plan,execution_fold=execution,undo_fold=undos)
    return {'plan_id':plan['plan_id'],'phase':current_phase(settings,plan['plan_id']),'unknown':[],
            'recovered':[e['op_id'] for _,e in proposed if e.get('outcome') in ('moved','archived','restored')],
            'notes': ['重建／修复日志：'+v for v in repair_notes],'undo_status':snap['undo_status']}


def restore_directories(settings,plan,hook=None,resume=True):
    """Directory barrier; return count only after ALL archived originals restored."""
    restored=0
    journal=Journal(settings.state_dir,plan['plan_id'],'undo')
    for op in sorted(plan['directory_operations'],key=lambda v:(v['rel_path'].count('/'),v['rel_path'])):
        execution,undos,runtime,_=verify_binding(settings,plan)
        slot=execution.get(op['op_id'],{});ur=undos.get(op['op_id'],{})
        result=slot.get('result') or {}
        if slot.get('intent') and not result: _error('目录悬空 intent，请先恢复')
        if result.get('outcome')!='archived': continue
        if ur.get('undo_result'):
            if ur['undo_result'].get('outcome')=='restored': continue
            _error('目录还原已有阻断结果')
        if ur.get('undo_skip'): _error('目录还原已有阻断结果')
        expected=[op['identity']['st_dev'],op['identity']['st_ino']]
        src=identity(op['source']);dst=identity(op['dest'])
        if ur.get('undo_intent') and src==expected and dst is None:
            _dir_event(journal,op,'undo_result',outcome='restored',reason_code='OK',undo_state='restored',reason='已还原，补记')
            continue
        if ur.get('undo_intent') and not resume: _error('未确认目录撤销续作，未移动')
        if src is not None or dst!=expected or names(op['dest']): _error('目录还原受阻，停止文件及更早批次')
        if not ur.get('undo_intent'):
            _dir_event(journal,op,'undo_intent',**{'from':op['dest'],'to':op['source'],'expected_identity':op['identity']})
        srcfd=open_checked(os.path.dirname(op['dest']),runtime[os.path.dirname(op['dest'])])
        dstfd=open_checked(os.path.dirname(op['source']),plan['dir_identities']['source_dirs'][op['parent_rel_path']])
        objfd=open_checked(op['dest'],expected)
        try:
            if os.listdir(objfd): _error('归档目录出现未知内容')
            if hook: hook('before_directory_restore',op)
            verify_binding(settings,plan)
            AtomicMover().rename_excl_at(srcfd,os.path.basename(op['dest']),dstfd,os.path.basename(op['source']))
            if hook: hook('after_directory_restore',op)
            raw_guard(plan['downloads_dir'],op['source'])
            if identity(op['source'])!=expected or os.path.lexists(op['dest']) or os.listdir(objfd): _error('原目录还原后异常，待检查')
            os.fsync(objfd);os.fsync(srcfd);os.fsync(dstfd)
            _dir_event(journal,op,'undo_result',outcome='restored',reason_code='OK',undo_state='restored',reason='原目录已还原')
            restored+=1
        finally:
            os.close(srcfd);os.close(dstfd);os.close(objfd)
    return restored
