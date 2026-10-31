"""Filename-only refinements, display hints, and immutable historical plans."""
import copy
import importlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from tests.harness import SandboxTestCase
from tests.legacy_material import fixture_bytes, fixture_paths
from doc_selecter import RULES_VERSION
from doc_selecter.rules import (classify_entry, file_subfolder, COURSE_TERMS,
                                decoded_filename, is_sensitive_name)
from doc_selecter.planner import (build_plan, write_plan, write_confirmation,
    assert_new_move_admissible, load_verified_plan, MoveAdmissionRefused, PlanError)
from doc_selecter.plan_hash import (compute_plan_hash, verify_plan_hash, PlanHashError,
                                   project_operation, canonical_bytes)
from doc_selecter.executor import execute_plan, recover_execution, ExecutionRefused
from doc_selecter.undo import undo_plan
from doc_selecter.ui_controller import Controller


class FinerClassificationTests(SandboxTestCase):
    def test_every_subfolder_and_fallback(self):
        cases = {
            'IMG_1234.HEIC':'手机照片', 'img_e1234 (1).JpG':'手机照片',
            'IMG_2.heif':'手机照片', 'IMG_2.jpeg':'手机照片', 'IMG_2.dng':'手机照片',
            'IMG_1234.PNG':'手机截屏', 'img_e2 (9).png':'手机截屏',
            '截屏2026-10-29 14.02.11.png':'电脑截屏',
            '屏幕快照 2026-10-29.png':'电脑截屏',
            'screenshot 2026-10-29.png':'电脑截屏', 'Screen Shot 2026-10-29.png':'电脑截屏',
            '微信图片_样例.jpg':'微信图片', 'MMEXPORTabcd.jpg':'微信图片',
            'IMG_x.png':'其他图片', 'IMG_1-edit.png':'其他图片', 'IMG_1 (a).jpg':'其他图片',
            'Screenshot说明.png':'其他图片', '图.webp':'其他图片', 'IMG_1.gif':'其他图片',
            'IMG_1234.MOV':'手机视频', 'img_e1234 (1).MP4':'手机视频',
            'IMG_1234.mkv':'视频', '片.mp4':'视频', '声.MP3':'音频', 'IMG_1.wav':'音频',
            '资料.PDF':'PDF', '资料.docm':'Word', '资料.XLSM':'Excel',
            '资料.pptm':'PPT', '资料.markdown':'Markdown', '资料.txt':'其他文档',
            '安装.dmg':'Mac 安装包（dmg pkg）', '安装.MPKG':'Mac 安装包（dmg pkg）',
            '安装.msixbundle':'Windows 安装包（exe msi）', '安装.EXE':'Windows 安装包（exe msi）',
            '安装.xapk':'手机安装包（apk）', '安装.APK':'手机安装包（apk）', '安装.whl':'其他安装包',
            '课程资料.zip':'ZIP', '包.RAR':'RAR 和 7Z', '包.7z':'RAR 和 7Z',
            '包.tar.gz':'其他压缩包', '包.tar.xz':'其他压缩包', '包.tar':'其他压缩包',
            '数据.csv':None, '不明.xyz':None,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                c = classify_entry(name, stat.S_IFREG)
                self.assertEqual(c.status, 'included')
                self.assertEqual(file_subfolder(name, c.category), expected)
                self.assertTrue(c.basis)

    def test_wechat_timestamp_names_are_organized_but_other_long_numbers_stay_excluded(self):
        for name in ('微信图片_20231015143022.jpg', '微信图片_20231015143022_1.jpg', 'mmexport1698123456789.jpg'):
            with self.subTest(name=name):
                self.assertFalse(is_sensitive_name(name))
                self.assertTrue(is_sensitive_name(name, legacy=True))
                c = classify_entry(name, stat.S_IFREG)
                self.assertEqual((c.status, file_subfolder(name, c.category)), ('included', '微信图片'))
        for name in ('微信图片_20231015143022_13812345678.jpg', '微信图片_2023101514302299.jpg',
                     '微信图片_20231015143022简历.jpg', 'mmexport16981234567890.jpg',
                     '图片_20231015143022.jpg', 'x微信图片_20231015143022.jpg'):
            with self.subTest(name=name):
                self.assertTrue(is_sensitive_name(name))
        self.sandbox.add_file('微信图片_20231015143022.jpg', b'ARTIFICIAL')
        plan = build_plan(self.sandbox.settings).plan
        op, = plan['operations']
        self.assertEqual(op['subfolder'], '微信图片')
        self.assertTrue(op['dest'].endswith('/图片截图/微信图片/微信图片_20231015143022.jpg'))

    def test_course_keywords_raw_and_encoded(self):
        for term in COURSE_TERMS:
            for name in (term+'样例.pdf', quote(term)+'样例.pdf'):
                with self.subTest(name=name):
                    self.assertEqual(file_subfolder(name, '文档资料'), '学校课程')
                    self.assertIn(term, classify_entry(name, stat.S_IFREG).basis)
        self.assertEqual(file_subfolder('课程资料.zip', '压缩包'), 'ZIP')

    def test_plan_top_level_only_and_recursive_destination(self):
        for name in ('IMG_2.png', '课设.pdf', '安装.dmg', '包.zip', '片.mp4', '数据.csv'):
            self.sandbox.add_file(name)
            self.sandbox.add_file('资料/'+name)
        plan = build_plan(self.sandbox.settings, recursive=True, scope_dirs=['资料']).plan
        self.assertEqual(plan['rules_version'], RULES_VERSION)
        for op in plan['operations']:
            self.assertNotIn('document_subcategory', op)
            if '/' in op['rel_path']:
                self.assertNotIn('subfolder', op)
                self.assertTrue(op['dest'].endswith(op['category']+'/'+op['rel_path']))
            elif op['category'] != '代码与数据':
                self.assertIn('subfolder', op)
                self.assertTrue(op['dest'].endswith(op['category']+'/'+op['subfolder']+'/'+op['rel_path']))
        assert_new_move_admissible(self.sandbox.settings, plan)

    def test_encoded_sensitive_excluded_without_persisted_names(self):
        names = [quote('密码')+'.pdf', quote('简历')+'.docx', '%74oken.txt']
        for name in names: self.sandbox.add_file(name)
        self.sandbox.add_file(quote('私密')+'/样例.pdf')
        plan = build_plan(self.sandbox.settings, recursive=True, scope_dirs=[quote('私密')]).plan
        self.assertFalse(plan['operations'])
        self.assertGreaterEqual(plan['counts']['sensitive_skipped'], 3)
        serialized = json.dumps(plan, ensure_ascii=False)
        for name in names+[quote('私密')]: self.assertNotIn(name, serialized)

    def test_preview_reads_no_file_body_or_exif(self):
        for name in ('IMG_1.heic','课设.pdf','安装.dmg','包.zip'):
            self.sandbox.add_file(name,b'artificial contents must not be opened')
        with patch('builtins.open',side_effect=AssertionError('body read forbidden')):
            plan=build_plan(self.sandbox.settings).plan
        self.assertEqual(len(plan['operations']),4)

    def test_new_field_hash_tamper_and_move_refused(self):
        self.sandbox.add_file('IMG_1234.png')
        plan = build_plan(self.sandbox.settings).plan
        write_plan(self.sandbox.settings, plan)
        write_confirmation(self.sandbox.settings, plan['plan_id'], plan['plan_hash'])
        op = plan['operations'][0]
        op['subfolder'] = '其他图片'
        self.assertFalse(verify_plan_hash(plan))
        self.sandbox.plan_file(plan['plan_id']).write_text(json.dumps(plan))
        with self.assertRaises((ExecutionRefused, PlanError, PlanHashError)):
            execute_plan(self.sandbox.settings, plan['plan_id'], plan['plan_hash'])
        self.assertTrue(Path(op['source']).is_file())
        self.assertFalse(Path(op['dest']).exists())

    def test_rehashed_name_category_basis_and_subfolder_tamper_refused(self):
        self.sandbox.add_file('IMG_1234.png')
        original = build_plan(self.sandbox.settings).plan
        for field, value in [('subfolder','其他图片'), ('subfolder',None),
                             ('category','文档资料'), ('classification_basis','伪造'),
                             ('document_subcategory','PDF')]:
            with self.subTest(field=field,value=value):
                plan=copy.deepcopy(original);plan['operations'][0][field]=value
                plan['plan_hash']=compute_plan_hash(plan)
                with self.assertRaises(PlanError): assert_new_move_admissible(self.sandbox.settings,plan)
        plan=copy.deepcopy(original);op=plan['operations'][0]
        op['rel_path']='资料.png';op['source']=op['source'].replace('IMG_1234','资料');op['dest']=op['dest'].replace('IMG_1234','资料')
        plan['plan_hash']=compute_plan_hash(plan)
        with self.assertRaises(PlanError): assert_new_move_admissible(self.sandbox.settings,plan)

    def test_new_layout_all_categories_execute_recover_undo(self):
        names=['IMG_1.jpg','IMG_1.mov','实验报告.pdf','安装.dmg','包.zip','数据.csv']
        for name in names:self.sandbox.add_file(name)
        plan=self.sandbox.make_plan()
        self.assertEqual(execute_plan(self.sandbox.settings,plan['plan_id'],plan['plan_hash']).moved,len(names))
        recover_execution(self.sandbox.settings,plan['plan_id'])
        self.assertEqual(undo_plan(self.sandbox.settings,plan['plan_id'],confirm=True).restored,len(names))
        for op in plan['operations']:self.assertIdentityMatches(op['source'],op['identity'])


class DisplayHintTests(SandboxTestCase):
    def rows(self, names, now=1800000000, recursive=False):
        for name, content, age in names:
            self.sandbox.add_file(name,content,mtime=now-age)
        c=Controller(self.sandbox.settings)
        self.addCleanup(c.close)
        c.plan=build_plan(c.settings,now=now,recursive=recursive,scope_dirs=['一','二'] if recursive else []).plan
        return c,{r['name']:r for r in c._rows(c.plan,c.history)}

    def test_duplicate_same_size_same_parent_only(self):
        c,rows=self.rows([('报告.pdf',b'ab',0),('报告 (1).pdf',b'cd',0),
            ('报告 (2).pdf',b'x',0),('报告(3).pdf',b'ab',0),('没有原件 (1).pdf',b'ab',0),
            ('一/跨层.pdf',b'ab',0),('二/跨层 (1).pdf',b'ab',0),
            ('一/同层.pdf',b'ab',0),('一/同层 (9).pdf',b'ab',0),('包.tar.gz',b'ab',0),('包 (1).tar.gz',b'ab',0)],recursive=True)
        for name,row in rows.items():
            self.assertEqual(bool(row['hints']),name in {'报告 (1).pdf','一/同层 (9).pdf','包 (1).tar.gz'},name)
        self.assertEqual(rows['报告 (1).pdf']['hints'][0]['text'],'可能和「报告.pdf」是同一个文件（大小一样）')

    def test_old_installer_day_boundary_and_plan_time(self):
        _,rows=self.rows([('三十.dmg',b'x',30*86400),('未满.dmg',b'x',30*86400-1),
                         ('四二.exe',b'x',42*86400+86399),('未来.apk',b'x',-86400),('旧.pdf',b'x',99*86400)])
        self.assertEqual(rows['三十.dmg']['hints'][0]['text'],'放了 30 天，可能已经装过')
        self.assertEqual(rows['四二.exe']['hints'][0]['text'],'放了 42 天，可能已经装过')
        for name in ('未满.dmg','未来.apk','旧.pdf'):self.assertEqual(rows[name]['hints'],[])

    def test_missing_preview_time_does_not_guess_age(self):
        c,_=self.rows([('安装.dmg',b'x',99*86400)])
        c.plan['created_at']='unknown'
        self.assertEqual(c._rows(c.plan,c.history)[0]['hints'],[])

    def test_encoded_name_strict_decode_and_original_unchanged(self):
        encoded=quote('人工报告')+'.pdf'
        c,rows=self.rows([(encoded,b'x',0),('%FF.pdf',b'x',0),('%ZZ.pdf',b'x',0),('正常.pdf',b'x',0)])
        self.assertEqual(rows[encoded]['display_name'],'人工报告.pdf')
        self.assertEqual(rows[encoded]['hints'][0]['kind'],'encoded')
        for name in ('%FF.pdf','%ZZ.pdf','正常.pdf'):
            self.assertEqual(rows[name]['display_name'],name);self.assertEqual(rows[name]['hints'],[])
        op=next(o for o in c.plan['operations'] if o['rel_path']==encoded)
        self.assertTrue(op['dest'].endswith(encoded));self.assertTrue(Path(op['source']).exists())

    def test_no_hints_reports_zero_and_no_warning_rows(self):
        c,rows=self.rows([('正常.pdf',b'x',0)])
        self.assertEqual(c.get_state()['hint_count'],0)
        self.assertFalse(rows['正常.pdf']['hints'])

    def test_hint_views_filter_count_and_details_leave_plan_and_hash_unchanged(self):
        c,_=self.rows([('安装.dmg',b'x',42*86400),('安装 (1).dmg',b'x',42*86400),('正常.pdf',b'x',0)])
        before=copy.deepcopy(c.plan);before_bytes=canonical_bytes(c.plan)
        self.assertEqual(c.get_state()['hint_count'],2)
        self.assertEqual(len(c.set_filter('hints')['rows']),2)
        self.assertEqual(len(c.set_filter('hints')['rows']),3)
        self.assertEqual(len(c.get_details()['rows']),3)
        self.assertEqual(c.plan,before);self.assertEqual(canonical_bytes(c.plan),before_bytes)
        self.assertEqual(compute_plan_hash(c.plan),before['plan_hash'])
        self.assertNotIn('hints',json.dumps(c.plan))


class HistoricalClassificationTests(SandboxTestCase):
    def frozen(self, commit):
        # Frozen sources are hash-checked before loading in an isolated package.
        package='frozen_'+commit[:8]
        root=self.sandbox.root/package;root.mkdir(exist_ok=True)
        for path in fixture_paths(commit):
            (root/Path(path).name).write_bytes(fixture_bytes(commit,path))
        spec=importlib.util.spec_from_file_location(package,root/'__init__.py',submodule_search_locations=[str(root)])
        module=importlib.util.module_from_spec(spec);sys.modules[package]=module;spec.loader.exec_module(module)
        self.addCleanup(lambda:[sys.modules.pop(k,None) for k in list(sys.modules) if k==package or k.startswith(package+'.')])
        return {n:importlib.import_module(package+'.'+n) for n in ('planner','plan_hash','executor','rules')}

    def test_frozen_fixture_hash_projection_recover_and_undo_unchanged(self):
        for commit in ('2df1a18927a6653a886e146e54b7984ddc4912d1','3b8b38029be069c6e36b394208c630c7f74e4437'):
            old=self.frozen(commit)
            for case in ('completed','interrupted'):
                with self.subTest(commit=commit,case=case):
                    name=commit[:5]+'-'+case+'.pdf';self.sandbox.add_file(name,b'artificial')
                    plan=old['planner'].build_plan(self.sandbox.settings).plan
                    self.assertEqual(compute_plan_hash(plan),plan['plan_hash'])
                    for i,op in enumerate(plan['operations']):
                        # No new field means identical canonical operation bytes.
                        self.assertEqual(canonical_bytes(project_operation(op,i)),canonical_bytes(old['plan_hash'].project_operation(op,i)))
                    old['planner'].write_plan(self.sandbox.settings,plan)
                    old['planner'].write_confirmation(self.sandbox.settings,plan['plan_id'],plan['plan_hash'])
                    before=self.sandbox.plan_file(plan['plan_id']).read_bytes()
                    mover = old['executor'].AtomicMover
                    rename = mover.rename_excl_at
                    def interrupted_move(instance, *args, **kwargs):
                        rename(instance, *args, **kwargs)
                        raise SystemExit('artificial interruption after atomic move')
                    if case == 'interrupted':
                        with patch.object(mover, 'rename_excl_at', interrupted_move):
                            with self.assertRaises(SystemExit):
                                old['executor'].execute_plan(self.sandbox.settings,plan['plan_id'],plan['plan_hash'])
                        self.assertFalse(Path(plan['operations'][0]['source']).exists())
                    else:
                        self.assertEqual(old['executor'].execute_plan(self.sandbox.settings,plan['plan_id'],plan['plan_hash']).moved, len(plan['operations']))
                    report = recover_execution(self.sandbox.settings,plan['plan_id'])
                    if case == 'interrupted':self.assertIn(plan['operations'][0]['op_id'], report['recovered'])
                    with self.assertRaises(MoveAdmissionRefused):load_verified_plan(self.sandbox.settings,plan['plan_id'],for_new_move=True)
                    result=undo_plan(self.sandbox.settings,plan['plan_id'],confirm=True)
                    self.assertGreaterEqual(result.restored,1)
                    self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(),before)
                    for op in plan['operations']:self.assertIdentityMatches(op['source'],op['identity'])
                    # Next fixture must not share the preceding plan's files.
                    for op in plan['operations']:Path(op['source']).unlink()

    def test_schema2_document_layout_frozen_hash_and_recover_undo(self):
        old=self.frozen('3b8b38029be069c6e36b394208c630c7f74e4437')
        self.sandbox.add_file('历史.pdf',b'artificial')
        plan=old['planner'].build_plan(self.sandbox.settings).plan
        plan['schema_version']=2;plan['rules_version']='milestone5-document-subcategories-r1'
        plan['plan_hash_alg']=old['plan_hash'].PLAN_HASH_ALG
        for field in ('archive','directory_operations'):plan.pop(field,None)
        plan['dir_identities'].pop('archive_dirs')
        plan['plan_hash']=old['plan_hash'].compute_plan_hash(plan)
        self.assertEqual(compute_plan_hash(plan),plan['plan_hash'])
        write_plan(self.sandbox.settings,plan)
        from doc_selecter.journal import make_event
        op=plan['operations'][0]
        self.sandbox.journal(plan['plan_id']).append(make_event('intent',op['op_id'],source=op['source'],dest=op['dest'],category=op['category'],identity=op['identity'],plan_hash=plan['plan_hash']))
        Path(op['dest']).parent.mkdir(parents=True,exist_ok=True);os.replace(op['source'],op['dest'])
        before=self.sandbox.plan_file(plan['plan_id']).read_bytes()
        recover_execution(self.sandbox.settings,plan['plan_id'])
        self.assertEqual(undo_plan(self.sandbox.settings,plan['plan_id'],confirm=True).restored,1)
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(),before)
        self.assertIdentityMatches(op['source'],op['identity'])

    def test_old_schema3_encoded_names_keep_historical_recovery_rules(self):
        old=self.frozen('3b8b38029be069c6e36b394208c630c7f74e4437')
        folder=quote('私密');name=folder+'/'+quote('密码')+'.pdf'
        self.sandbox.add_file(name,b'artificial historical sample')
        plan=old['planner'].build_plan(self.sandbox.settings,recursive=True,scope_dirs=[folder],archive_emptied_dirs=True).plan
        old['planner'].write_plan(self.sandbox.settings,plan)
        old['planner'].write_confirmation(self.sandbox.settings,plan['plan_id'],plan['plan_hash'])
        result=old['executor'].execute_plan(self.sandbox.settings,plan['plan_id'],plan['plan_hash'])
        self.assertEqual(result.moved,1)
        before=self.sandbox.plan_file(plan['plan_id']).read_bytes()
        recover_execution(self.sandbox.settings,plan['plan_id'])
        self.assertEqual(undo_plan(self.sandbox.settings,plan['plan_id'],confirm=True).restored,1)
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(),before)
        self.assertIdentityMatches(plan['operations'][0]['source'],plan['operations'][0]['identity'])

    def test_schema4_keeps_frozen_classification_and_recovery(self):
        from doc_selecter import remaining_archive as remaining
        old=self.frozen('3b8b38029be069c6e36b394208c630c7f74e4437')
        self.sandbox.add_file('课程.pdf',b'artificial remaining sample')
        # Generate the persisted contract with the original frozen classifier.
        def classifier(name, mode, **kwargs):return old['rules'].classify_entry(name,mode)
        with patch.object(remaining,'classify_entry',classifier):
            plan=remaining.build_remaining_plan(self.sandbox.settings,selection=['课程.pdf']).plan
        self.assertEqual(plan['rules_version'],'milestone5-remaining-archive-r1')
        remaining.validate_structure(self.sandbox.settings,plan)
        self.assertNotIn('subfolder',plan['operations'][0])
        write_plan(self.sandbox.settings,plan)
        write_confirmation(self.sandbox.settings,plan['plan_id'],plan['plan_hash'])
        before=self.sandbox.plan_file(plan['plan_id']).read_bytes()
        self.assertEqual(execute_plan(self.sandbox.settings,plan['plan_id'],plan['plan_hash']).moved,1)
        recover_execution(self.sandbox.settings,plan['plan_id'])
        self.assertEqual(undo_plan(self.sandbox.settings,plan['plan_id'],confirm=True).restored,1)
        self.assertEqual(self.sandbox.plan_file(plan['plan_id']).read_bytes(),before)
