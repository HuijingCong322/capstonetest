import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import b_profile_phase1 as p


def doc(day, code, identity):
    return {'official_date': day, 'doc_code': code, 'doc_id': identity, 'direction': 'OUTGOING' if code.startswith('CT') or code=='NOA' else 'INCOMING'}


def fixture_docs():
    return [doc('2013-01-01','CTNF','o1'),doc('2013-02-01','REM','r1'),doc('2013-03-01','CTFR','o2')]


def test_missing_builder_is_not_silently_accepted():
    assert callable(getattr(p, 'pair_candidates', None))


def test_pairs_require_strict_per_case_cutoff_and_verified_both_actions():
    docs=fixture_docs();signatures={'o1':True,'o2':True}
    assert p.pair_candidates(docs,signatures,'2013-03-01')==[]
    assert p.pair_candidates(docs,signatures,'2013-03-02')[0]['next_doc_id']=='o2'
    assert p.pair_candidates(docs,{'o1':True,'o2':False},'2014-01-01')==[]
    assert p.pair_candidates(docs,{'o1':True},'2014-01-01')==[]
    assert p.pair_candidates(docs,{'o1':False,'o2':True},'2014-01-01')==[]


def test_pairing_does_not_skip_unverified_noa_or_intervening_letters():
    for code in ['NOA','CTAV','CTEQ','CTMS','EXIN','CTRS']:
        docs=fixture_docs()+[doc('2013-02-15',code,'intervening')]
        assert p.pair_candidates(docs,{'o1':True,'o2':True},'2014-01-01')==[]


def test_ambiguous_same_day_and_missing_mailed_document_block_pair():
    docs=fixture_docs()+[doc('2013-03-01','CTNF','other')]
    assert p.pair_candidates(docs,{'o1':True,'o2':True,'other':True},'2014-01-01')==[]
    transactions=[{'event_date':'2013-02-15','event_code':'MCTNF'}]
    assert p.pair_candidates(fixture_docs(),{'o1':True,'o2':True},'2014-01-01',transactions)==[]


def test_retrieval_ignores_outcome_and_selects_distinct_families():
    candidates=[{'episode_id':'a','app_id':'1','family_root':'f1','retrieval_text':'wireless antenna','next_code':'CTFR'},
                {'episode_id':'b','app_id':'2','family_root':'f1','retrieval_text':'wireless antenna','next_code':'CTNF'},
                {'episode_id':'c','app_id':'3','family_root':'f2','retrieval_text':'plant seed','next_code':'CTFR'}]
    ranked=p.retrieve('wireless antenna',candidates,3)
    assert [v['episode_id'] for v in ranked]==['a','c']
    candidates[0]['next_code']='NOA';candidates[0]['summary_lines']=['secret gold','target outcome']
    assert [v['episode_id'] for v in p.retrieve('wireless antenna',candidates,3)]==['a','c']


def test_query_uses_observed_inputs_only_and_rejects_future(tmp_path):
    (tmp_path/'before.txt').write_text('antenna reply');(tmp_path/'future.txt').write_text('SECRET')
    e={'cutoff':'2013-02-01','inputs':[{'date':'2013-02-01','text_path':'before.txt','doc_id':'a','doc_code':'REM'}],
       'target':{'text_path':'future.txt'},'labels':{'next_type':'SECRET'}}
    assert 'SECRET' not in p.query_text(tmp_path,e)
    e['inputs'].append({'date':'2013-03-01','text_path':'future.txt','doc_id':'b','doc_code':'REM'})
    with pytest.raises(ValueError,match='Future'):
        p.query_text(tmp_path,e)


def test_profile_is_explicitly_draft_and_hard_capped():
    class Encoding:
        def encode(self,text,**kw):return list(text.encode())
        def decode(self,tokens):return bytes(tokens).decode(errors='ignore')
    case={'sample_id':'test','cutoff':'2014-01-01','examiner_id':'ex_own','control_examiner_id':'ex_wrong','control_art_unit':'1'}
    episode={'episode_id':'e','app_id':'1','summary_lines':['a'*10000,'b'*10000]}
    text,n=p.render_profile(case,'own',[episode],Encoding(),max_tokens=8000)
    assert n<=8000 and 'pending D' in text and 'draft' in text.lower()
    assert text.count('Past case')==1


def test_unknown_setting_fails_closed():
    with pytest.raises(ValueError):
        p.render_profile({},'whatever',[],None)


def test_missing_reply_text_is_explicit_not_fabricated(tmp_path):
    import csv
    folder=tmp_path/'apps/1';text=folder/'text';text.mkdir(parents=True)
    for filename,body in [('2013-01-01_CTNF_o1.txt','The antenna claims are rejected over a wireless reference.'),
                          ('2013-03-01_CTFR_o2.txt','The claims remain rejected under section 103.')]:
        (text/filename).write_text(body)
    for filename,data,fields in [('catalog.csv',fixture_docs(),['official_date','doc_code','doc_id','direction']),
                                 ('transactions.csv',[],['event_date','event_code','event_description'])]:
        with (folder/filename).open('w') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(data)
    index=[{'app_id':'1','examiner_id':'ex_a','family_root':'f1'}]
    actions=[{'app_id':'1','examiner_id':'ex_a','doc_id':v,'signed_by_this_examiner':'true'} for v in ['o1','o2']]
    episodes,audit=p.build_episodes(tmp_path,index,actions,{'ex_a':'2014-01-01'})
    assert len(episodes)==1
    assert episodes[0]['reply_text_status']=='unavailable'
    assert 'unavailable' in episodes[0]['summary_lines'][0]
    assert 'antenna' in episodes[0]['retrieval_text']
    assert 'section 103' not in episodes[0]['retrieval_text']


def test_signature_redaction_preserves_dates_and_claim_fractions():
    text='Applicant amendments 10/03/2013 concern a 1/2 ratio.\n/EXAMINER NAME/\nPrimary Examiner, Art Unit 1'
    sanitized=p.scrub(text)
    assert '10/03/2013' in sanitized and '1/2' in sanitized
    assert 'EXAMINER NAME' not in sanitized
