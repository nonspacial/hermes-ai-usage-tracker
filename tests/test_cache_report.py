"""Offline read-only report regressions, including mixed legacy/current evidence."""
import importlib.util,json,sqlite3
from pathlib import Path
import pytest
from _hermes_ai_usage_ledger_v2.storage import Store
spec=importlib.util.spec_from_file_location('_cache_report',Path(__file__).resolve().parents[1]/'cache_write_report.py')
reporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(reporter)

RAW={'input_tokens':136408,'input_tokens_details':{'cached_tokens':135936,'cache_write_tokens':0},'output_tokens':174,'total_tokens':136582}

def insert(s,key,usage,evidence=None):
    data={'id':key,'started':100,'ended':101,'provider':'openai-codex','model':'test-model','status':'completed','usage':usage}
    if evidence:data['cache_evidence']=evidence
    with s.db() as c:c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',(key,100,101,'openai-codex','test-model','s1','main',None,'completed',json.dumps(data)))

def test_report_classifies_sources_not_sum_all_events(tmp_path):
    s=Store(tmp_path)
    insert(s,'legacy',{'cache_write_tokens':0,'raw_usage':None})
    insert(s,'sdk',{'cache_write_tokens':0,'raw_usage':RAW})
    insert(s,'wire',{'cache_write_tokens':0,'raw_usage':RAW},{'boundary':'decoded_http_json_before_sdk_models','cache_write_state':'explicit_zero','usage':RAW,'endpoint_kind':'chatgpt_codex_subscription'})
    with s.db() as c:
        before=list(c.execute('SELECT data FROM requests'))
        for _ in range(4):s.event(c,'duplicate_observation','wire',{'cache_write_tokens':999})
    result=reporter.report(s.path,0,now=200)
    assert result['request_records']==3
    assert result['classification']=={'normalized_only:not_provider_verified':1,'sdk_or_adapter_usage:explicit_zero':1,'pre_sdk_http_json:explicit_zero':1}
    assert result['models'][0]['pre_sdk_write_tokens']==0 and result['models'][0]['requests']==3
    assert len(result['pre_sdk_examples'])==1
    with s.db() as c:assert [x[0] for x in c.execute('SELECT data FROM requests')]==[x[0] for x in before]

def test_report_missing_cache_field_and_positive_write(tmp_path):
    s=Store(tmp_path)
    missing={**RAW,'input_tokens_details':{'cached_tokens':135936}}
    positive={**RAW,'input_tokens_details':{'cached_tokens':135936,'cache_write_tokens':50}}
    insert(s,'missing',{'raw_usage':missing,'cache_write_tokens':None},{'boundary':'decoded_http_json_before_sdk_models','cache_write_state':'field_absent','usage':missing})
    insert(s,'positive',{'raw_usage':positive,'cache_write_tokens':50},{'boundary':'decoded_http_json_before_sdk_models','cache_write_state':'positive','usage':positive})
    data=reporter.report(s.path,0,now=200)
    assert data['models'][0]['pre_sdk_write_tokens']==50
    assert data['classification']['pre_sdk_http_json:field_absent']==1
    assert data['pre_sdk_vs_current_write_mismatches']==0

def test_report_lookback_and_atomic_no_overwrite(tmp_path):
    s=Store(tmp_path);insert(s,'old',{'cache_write_tokens':0})
    assert reporter.report(s.path,1,now=5000)['request_records']==0
    out=tmp_path/'report.json'
    assert reporter.main(['--home',str(tmp_path),'--hours','0','--out',str(out)])==0
    original=out.read_bytes()
    assert reporter.main(['--home',str(tmp_path),'--out',str(out)])==1
    assert out.read_bytes()==original

def test_missing_db_never_creates(tmp_path):
    db=tmp_path/'missing.sqlite3'
    with pytest.raises(FileNotFoundError):reporter.report(db)
    assert not db.exists()

@pytest.mark.parametrize('v',[float('inf'),float('nan'),-1,True,'0',None])
def test_invalid_numbers_are_not_token_counts(v):
    assert reporter.num(v) is None
