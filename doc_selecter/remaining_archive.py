"""Explicit remaining-item archive (v4); no old exclusion list grants permission.

Only names and metadata are read. The versioned directory engine provides
durable layout identities, locking, recovery and restoration of original objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import stat
import time

from .atomic_move import FileIdentity, DirectoryIdentityChanged
from .directory_archive import chains, identity, nfc, open_checked, raw_guard
from .gui_source import selected_settings
from .plan_hash import compute_plan_hash, new_plan_id, project_identity
from .rules import (CATEGORIES, classify_entry, is_code_project_dir,
                    is_package_dir, is_sensitive_relative_path, is_temp_download)
from .scanner import normalize_scope

RULES_VERSION = 'milestone5-remaining-archive-r1'
HASH_ALG = 'sha256/canonical-json-v3'
LAYOUT_VERSION = 'remaining-slots-v1'


def refuse(message):
    from .planner import PlanError
    raise PlanError(message)


def relative(value, *, empty=False):
    if (not isinstance(value, str) or (not value and not empty) or '\x00' in value
            or (value and any(p in ('', '.', '..') for p in value.split('/')))):
        refuse('剩余归档相对路径非法')
    if is_sensitive_relative_path(value):
        refuse('剩余归档含受保护名称；请重新预览（不展示名称）')
    if value and any(p.startswith('.') or is_package_dir(p) or is_temp_download(p)
                     for p in value.split('/')):
        refuse('剩余归档含隐藏、包或临时路径')
    return nfc(value)


def authorized(rel, scopes, recursive):
    return bool(recursive and any(s and (rel == s or rel.startswith(s + '/')) for s in scopes))


def candidate_in_scope(rel, kind, scopes, recursive):
    parent = rel.rpartition('/')[0]
    return (not parent or authorized(parent, scopes, recursive)
            or (kind == 'empty_directory' and authorized(rel, scopes, recursive)))


@dataclass
class Discovery:
    selected_scope: list[str]
    recursive: bool
    scan_scope: list[str] = field(default_factory=lambda: [''])
    candidates: list[dict] = field(default_factory=list)
    excluded_counts: dict[str, int] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)


def discover_remaining(settings, *, scope_dirs=None, recursive=False):
    """No-follow enumeration. Unselected trees are never walked for their files."""
    if type(recursive) is not bool:
        refuse('递归开关非法')
    scopes = normalize_scope(scope_dirs if recursive else [])
    scopes = sorted({relative(s, empty=True) for s in scopes})
    if any(s.split('/')[0] == settings.target_parent_name for s in scopes if s):
        refuse('分类区域不能作为剩余归档来源')
    # This is the same trusted source gate as the GUI, before metadata parsing.
    checked = selected_settings(settings, settings.downloads_dir)
    root = checked.canonical_downloads_dir
    result = Discovery(scopes, recursive)

    def exclude(reason):
        result.excluded_counts[reason] = result.excluded_counts.get(reason, 0) + 1

    def walk(fd, rel, depth):
        if depth > 32:
            result.issues.append({'path': rel, 'reason': '超过安全扫描深度'})
            return
        names = os.listdir(fd)
        if len(set(map(nfc, names))) != len(names):
            refuse('目录名称NFC冲突或歧义')
        if is_code_project_dir(frozenset(names))[0]:
            if not rel:
                refuse('来源是受保护成套项目，不能归档其成员')
            exclude('受保护成套项目')
            return
        if rel and authorized(nfc(rel), scopes, recursive):
            result.scan_scope.append(nfc(rel))
        for name in sorted(names):
            child = rel + '/' + name if rel else name
            if is_sensitive_relative_path(child):
                exclude('疑似私人项目')
                continue  # NEVER stat or persist sensitive names.
            if not rel and name == settings.target_parent_name:
                exclude('已有分类与归档区域')
                continue
            if name.startswith('.') or is_package_dir(name) or is_temp_download(name):
                exclude('隐藏、包或未完成项目')
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    exclude('链接')
                    continue
                if stat.S_ISDIR(info.st_mode):
                    # A top-level directory may be listed as an EMPTY candidate,
                    # but nonempty unselected trees have no member permission.
                    transit = recursive and any(s.startswith(nfc(child) + '/') for s in scopes)
                    can_enter = authorized(nfc(child), scopes, recursive)
                    nested = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        current = os.fstat(nested)
                        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                            refuse('候选扫描期间目录身份变化')
                        members = os.listdir(nested)
                        if not members and candidate_in_scope(nfc(child), 'empty_directory', scopes, recursive):
                            result.candidates.append({'kind': 'empty_directory', 'rel_path': nfc(child),
                                'source': root + '/' + nfc(child), 'identity': {
                                    'st_dev': info.st_dev, 'st_ino': info.st_ino, 'mode_type': 'dir'}})
                            if can_enter:
                                result.scan_scope.append(nfc(child))
                        elif is_code_project_dir(frozenset(members))[0]:
                            exclude('受保护成套项目')
                        elif can_enter or transit:
                            walk(nested, child, depth + 1)
                        else:
                            exclude('未选择的非空文件夹')
                    finally:
                        os.close(nested)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    exclude('特殊类型')
                    continue
                if rel and not authorized(nfc(rel), scopes, recursive):
                    continue  # Transit ancestors do not grant file permission.
                classification = classify_entry(name, info.st_mode)
                if not classification.included:
                    exclude('受保护文件')
                    continue
                result.candidates.append({'kind': 'file', 'rel_path': nfc(child),
                    'source': root + '/' + nfc(child), 'category': classification.category,
                    'classification_basis': classification.basis,
                    'identity': FileIdentity(info.st_dev, info.st_ino, info.st_size,
                                             info.st_mtime_ns, 'regular').as_dict()})
            except OSError as exc:
                result.issues.append({'path': nfc(child), 'reason': type(exc).__name__})
        if sorted(names) != sorted(os.listdir(fd)):
            result.issues.append({'path': nfc(rel), 'reason': '扫描期间成员变化'})

    fd = open_checked(root)
    try:
        walk(fd, '', 0)
    finally:
        os.close(fd)
    result.scan_scope = sorted(set(result.scan_scope))
    result.candidates.sort(key=lambda c: (c['rel_path'], c['kind']))
    # Explicit paths which did not reach an authorized directory are not silently ignored.
    if any(s and s not in result.scan_scope for s in scopes):
        result.issues.append({'path': '', 'reason': '所选范围未完整进入；可能受保护、缺失或读取失败'})
    return result


def capture_identities(settings, plan):
    """Capture every selected source/target chain; no inferred missing keys."""
    di = {'downloads_dir': identity(plan['downloads_dir']), 'target_root': None,
          'source_dirs': {}, 'dest_dirs': {}, 'archive_dirs': {}}
    raw_guard(plan['downloads_dir'], plan['target_root'])
    di['target_root'] = identity(plan['target_root'])
    di['source_dirs'][''] = di['downloads_dir']
    for op in plan['operations'] + plan['directory_operations']:
        rel = op['rel_path'] if op['kind'] == 'empty_directory' else op['rel_path'].rpartition('/')[0]
        for parent in chains(rel):
            path = plan['downloads_dir'] + ('/' + parent if parent else '')
            raw_guard(plan['downloads_dir'], path)
            di['source_dirs'][parent] = identity(path)
        target_rel = os.path.relpath(os.path.dirname(op['dest']), plan['target_root'])
        field_name = 'archive_dirs' if op['kind'] == 'empty_directory' else 'dest_dirs'
        for parent in chains(target_rel)[1:]:
            path = plan['target_root'] + '/' + parent
            raw_guard(plan['downloads_dir'], path)
            di[field_name][parent] = identity(path)
    return di


def build_remaining_plan(settings, *, selection=None, scope_dirs=None, recursive=False,
                         expected_candidates=None, plan_id=None):
    """Always rediscover; selection consists of explicit relative paths, never exclusions."""
    from .planner import PlanBuild
    discovery = discover_remaining(settings, scope_dirs=scope_dirs, recursive=recursive)
    if discovery.issues:
        refuse('剩余候选读取失败、扫描变化或范围不完整；请重新预览')
    if expected_candidates is not None and discovery.candidates != expected_candidates:
        refuse('候选或身份已变化，请重新发现后再勾选')
    selected = [relative(s) for s in selection or []]
    if len(set(selected)) != len(selected):
        refuse('归档勾选重复或NFC歧义')
    candidates = {c['rel_path']: c for c in discovery.candidates}
    if any(s not in candidates for s in selected):
        refuse('所选项目不是当前合规候选；请重新预览')
    batch = plan_id or new_plan_id()
    prefix = settings.target_parent_name + '/已归档/' + batch
    files, dirs = [], []
    for rel in sorted(selected):
        item = candidates[rel]
        op = dict(item, identity=dict(item['identity']))
        if item['kind'] == 'file':
            op['op_id'] = f'op-{len(files)+1:04d}'
            op['dest'] = settings.canonical_downloads_dir + '/' + prefix + '/剩余文件/' + rel
            files.append(op)
        else:
            op.update(op_id=f'dir-{len(dirs)+1:04d}', parent_rel_path=rel.rpartition('/')[0],
                      preview_nonempty=False, enumeration_complete=True,
                      direct_members=[], depends_on=[])
            op['dest'] = settings.canonical_downloads_dir + '/' + prefix + '/空文件夹/' + op['op_id'] + '/' + rel.rsplit('/', 1)[-1]
            dirs.append(op)
    plan = {'schema_version': 4, 'plan_type': 'remaining_archive', 'plan_id': batch,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'rules_version': RULES_VERSION,
        'plan_hash_alg': HASH_ALG, 'downloads_dir': settings.canonical_downloads_dir,
        'target_root': settings.canonical_target_root, 'category_selection': list(CATEGORIES),
        'recursive': recursive, 'selected_scope': discovery.selected_scope,
        'scan_scope': discovery.scan_scope, 'remaining_candidates': discovery.candidates,
        'remaining_selection': sorted(selected), 'operations': files, 'directory_operations': dirs,
        'archive': {'enabled': True, 'batch_id': batch, 'archive_layout_version': LAYOUT_VERSION,
                    'relative_root': prefix + '/空文件夹'},
        'counts': {'selected': len(files), 'selected_dirs': len(dirs), 'included': len(discovery.candidates)},
        'excluded': {'疑似敏感项': {'count': discovery.excluded_counts.get('疑似私人项目', 0), 'names': None},
                     '未归档原因数量': discovery.excluded_counts}}
    plan['dir_identities'] = capture_identities(settings, plan)
    validate_structure(settings, plan)
    plan['plan_hash'] = compute_plan_hash(plan)
    return PlanBuild(plan, discovery)


def validate_structure(settings, plan):
    """Pure strings/types only: called BEFORE fingerprint or path metadata."""
    from .planner import normalized_dir_identity_map
    if (plan.get('schema_version') != 4 or plan.get('rules_version') != RULES_VERSION
            or plan.get('plan_hash_alg') != HASH_ALG or plan.get('plan_type') != 'remaining_archive'):
        refuse('剩余归档版本或语义不兼容')
    if plan.get('downloads_dir') != settings.canonical_downloads_dir or plan.get('target_root') != settings.canonical_target_root:
        refuse('剩余归档根与可信配置不一致')
    batch = plan.get('plan_id')
    if not isinstance(batch, str) or not re.fullmatch(r'[a-z0-9-]+', batch):
        refuse('剩余归档批次非法')
    prefix = settings.target_parent_name + '/已归档/' + batch
    expected_archive = {'enabled': True, 'batch_id': batch, 'archive_layout_version': LAYOUT_VERSION,
                        'relative_root': prefix + '/空文件夹'}
    if plan.get('archive') != expected_archive or type(plan['archive']['enabled']) is not bool:
        refuse('剩余归档固定根或授权被改写')
    scopes, actual = plan.get('selected_scope'), plan.get('scan_scope')
    if not isinstance(scopes, list) or not isinstance(actual, list) or '' not in scopes or '' not in actual or type(plan.get('recursive')) is not bool:
        refuse('剩余归档范围非法')
    for s in scopes + actual:
        if relative(s, empty=True) != s or (s and s.split('/')[0] == settings.target_parent_name):
            refuse('剩余归档范围含保护区域或非规范路径')
    if scopes != sorted(set(scopes)) or actual != sorted(set(actual)):
        refuse('归档范围重复')
    if not plan['recursive'] and (scopes != [''] or actual != ['']):
        refuse('归档递归授权不一致')
    if any(s and not authorized(s, scopes, plan['recursive']) for s in actual):
        refuse('归档实际范围超出明确授权')
    if plan.get('category_selection') != list(CATEGORIES):
        refuse('归档类别字段非法；不复用分类勾选授权')
    candidates = plan.get('remaining_candidates')
    selected = plan.get('remaining_selection')
    files, dirs = plan.get('operations'), plan.get('directory_operations')
    if not all(isinstance(v, list) for v in (candidates, selected, files, dirs)):
        refuse('剩余候选／勾选／操作字段非法')
    by_rel = {}
    for c in candidates:
        if not isinstance(c, dict):
            refuse('候选非法')
        rel = relative(c.get('rel_path')); kind = c.get('kind')
        if rel != c['rel_path'] or rel in by_rel or kind not in {'file', 'empty_directory'}:
            refuse('候选重复、编码歧义或类型非法')
        if rel.split('/')[0] == settings.target_parent_name or not candidate_in_scope(rel, kind, scopes, plan['recursive']):
            refuse('候选不在授权范围')
        if c.get('source') != plan['downloads_dir'] + '/' + rel:
            refuse('候选原路径被改写')
        expected_keys = {'kind', 'rel_path', 'source', 'identity'}
        if kind == 'file':
            expected_keys.update(('category', 'classification_basis'))
            classification = classify_entry(rel.rsplit('/', 1)[-1], stat.S_IFREG)
            if not classification.included or classification.category != c.get('category') or classification.basis != c.get('classification_basis'):
                refuse('候选筛查或分类依据不符合当前规则')
            projected = project_identity(c.get('identity', {}), '候选身份')
            if set(c['identity']) != {'st_dev', 'st_ino', 'size_bytes', 'mtime_ns', 'mode_type'}:
                refuse('普通文件身份字段非法')
            if projected['mode_type'] != 'regular' or any(projected[k] < 0 for k in ('st_dev', 'st_ino', 'size_bytes', 'mtime_ns')):
                refuse('普通文件身份非法')
        else:
            ident = c.get('identity')
            if not isinstance(ident, dict) or set(ident) != {'st_dev', 'st_ino', 'mode_type'} or ident['mode_type'] != 'dir' or any(type(ident[k]) is not int or ident[k] < 0 for k in ('st_dev', 'st_ino')):
                refuse('原空目录身份非法')
        if set(c) != expected_keys:
            refuse('候选字段与固定类型不符')
        by_rel[rel] = c
    if selected != sorted({relative(s) for s in selected}) or any(s not in by_rel for s in selected):
        refuse('勾选集合不是有效候选子集')
    ops = files + dirs
    if any(not isinstance(op, dict) for op in ops):
        refuse('归档操作非法')
    if len({op.get('op_id') for op in ops}) != len(ops) or sorted(op.get('rel_path', '') for op in ops) != selected:
        refuse('归档操作与明确勾选不一致')
    expected_source, expected_dest, expected_dirs = {''}, set(), set()
    dir_pairs = set()
    for op in ops:
        rel = relative(op.get('rel_path')); c = by_rel[rel]
        if any(op.get(k) != v for k, v in c.items()):
            refuse('操作与候选身份／路径不一致')
        if op in files:
            if c['kind'] != 'file' or not re.fullmatch(r'op-[0-9]{4,}', str(op.get('op_id'))):
                refuse('文件操作类型／ID非法')
            dest = plan['downloads_dir'] + '/' + prefix + '/剩余文件/' + rel
            expected_source.update(chains(rel.rpartition('/')[0]))
            expected_dest.update(chains(os.path.relpath(os.path.dirname(dest), plan['target_root']))[1:])
            if set(op) != set(c) | {'op_id', 'dest'}:
                refuse('剩余文件操作含额外权限字段')
        else:
            if c['kind'] != 'empty_directory' or not re.fullmatch(r'dir-[0-9]{4,}', str(op.get('op_id'))):
                refuse('空目录操作类型／ID非法')
            if (op.get('parent_rel_path') != rel.rpartition('/')[0] or op.get('preview_nonempty') is not False
                    or op.get('enumeration_complete') is not True or op.get('direct_members') != [] or op.get('depends_on') != []):
                refuse('原本空目录不能获取非空／因果归档权限')
            if set(op) != set(c) | {'op_id', 'dest', 'parent_rel_path', 'preview_nonempty', 'enumeration_complete', 'direct_members', 'depends_on'}:
                refuse('空目录字段非法')
            pair = (op['identity']['st_dev'], op['identity']['st_ino'])
            if pair in dir_pairs:
                refuse('空目录原对象重复')
            dir_pairs.add(pair)
            dest = plan['downloads_dir'] + '/' + prefix + '/空文件夹/' + op['op_id'] + '/' + rel.rsplit('/', 1)[-1]
            expected_source.update(chains(rel))
            expected_dirs.update(chains(os.path.relpath(os.path.dirname(dest), plan['target_root']))[1:])
        if op.get('dest') != dest:
            refuse('归档目标不是固定批次／原相对层级／独立槽位')
    di = plan.get('dir_identities')
    if not isinstance(di, dict) or set(di) != {'downloads_dir', 'target_root', 'source_dirs', 'dest_dirs', 'archive_dirs'}:
        refuse('归档父链身份字段缺失')
    for field_name, keys in (('source_dirs', expected_source), ('dest_dirs', expected_dest), ('archive_dirs', expected_dirs)):
        values = normalized_dir_identity_map(di.get(field_name) or {}, field_name)
        if set(values) != keys:
            refuse('归档父链缺失或包含额外授权')
        for value in values.values():
            if value is None and field_name == 'source_dirs':
                refuse('来源目录缺少身份')
            if value is not None and (not isinstance(value, list) or len(value) != 2 or any(type(v) is not int for v in value)):
                refuse('归档父链身份非法')
    if di['downloads_dir'] != di['source_dirs'].get(''):
        refuse('归档来源根身份不一致')
    if di['target_root'] is not None and (not isinstance(di['target_root'], list)
            or len(di['target_root']) != 2 or any(type(v) is not int or v < 0 for v in di['target_root'])):
        refuse('归档目标根身份非法')
    for op in dirs:
        if di['source_dirs'].get(op['rel_path']) != [op['identity']['st_dev'], op['identity']['st_ino']]:
            refuse('原空目录与父链身份不一致')


def live_admission(settings, plan, execution):
    """Current project protection and pre-first-intent candidate recheck.

    Once operation evidence exists, recovery uses only its immutable identities;
    vanished files are not rediscovered to manufacture a new baseline.
    """
    for rel in plan['dir_identities']['source_dirs']:
        path = plan['downloads_dir'] + ('/' + rel if rel else '')
        # Archived empty directories may be absent; versioned projection checks them.
        if any(op['rel_path'] == rel and execution.get(op['op_id'], {}).get('intent')
               for op in plan['directory_operations']):
            continue
        try:
            fd = open_checked(path, plan['dir_identities']['source_dirs'][rel])
        except DirectoryIdentityChanged as exc:
            refuse(str(exc))
        try:
            if is_code_project_dir(frozenset(os.listdir(fd)))[0]:
                refuse('当前来源父链已成为受保护成套项目')
        finally:
            os.close(fd)
    if not any(slot.get('intent') for key, slot in execution.items() if not key.startswith('layout-')):
        discovery = discover_remaining(settings, scope_dirs=plan['selected_scope'], recursive=plan['recursive'])
        if discovery.issues or discovery.candidates != plan['remaining_candidates']:
            refuse('当前候选或身份已变化，请重新生成归档预览')


def rebase_unpublished(settings, plan, batch):
    """A write collision creates a new unpublished batch, never edits historical plans."""
    old = plan['archive']['batch_id']
    plan['plan_id'] = batch
    plan['archive']['batch_id'] = batch
    plan['archive']['relative_root'] = f'{settings.target_parent_name}/已归档/{batch}/空文件夹'
    for op in plan['operations'] + plan['directory_operations']:
        before = plan['target_root'] + '/已归档/' + old + '/'
        after = plan['target_root'] + '/已归档/' + batch + '/'
        op['dest'] = after + op['dest'][len(before):]
    plan['dir_identities'] = capture_identities(settings, plan)
    validate_structure(settings, plan)
    plan['plan_hash'] = compute_plan_hash(plan)
