"""Controlled r11 generator from hash-verified frozen source fixtures."""
import hashlib
import json
import types
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'legacy'
MANIFEST = json.loads((FIXTURES / 'manifest.json').read_text())

def fixture_bytes(commit, path):
    expected = MANIFEST[commit + ':' + path]
    data = (FIXTURES / commit / path).read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected, 'Legacy fixture changed'
    return data

def fixture_paths(commit):
    return [key.split(':', 1)[1] for key in MANIFEST if key.startswith(commit + ':')]

COMMIT='2df1a18927a6653a886e146e54b7984ddc4912d1'

def r11_planner():
    modules={}
    for name in ('__init__','plan_hash','planner'):
        path='doc_selecter/'+name+'.py'
        source=fixture_bytes(COMMIT, path).decode('utf-8')
        module=types.ModuleType('doc_selecter._frozen_'+name)
        module.__package__='doc_selecter'
        sys.modules[module.__name__]=module
        exec(compile(source,COMMIT+':'+path,'exec'),module.__dict__)
        modules[name]=module
    p=modules['planner']; h=modules['plan_hash']
    p.RULES_VERSION=modules['__init__'].RULES_VERSION
    p.PLAN_SCHEMA_VERSION=h.PLAN_SCHEMA_VERSION;p.PLAN_HASH_ALG=h.PLAN_HASH_ALG
    p.compute_plan_hash=h.compute_plan_hash;p.verify_plan_hash=h.verify_plan_hash
    assert p.RULES_VERSION=='milestone4-privacy-r11' and p.PLAN_SCHEMA_VERSION==2
    return p
