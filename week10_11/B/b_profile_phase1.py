"""B phase 1: cutoff-safe draft retrieval profiles; D statistics and C review pending."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from datetime import date
import hashlib
import html
import json
import os
from pathlib import Path
import re
from sklearn.feature_extraction.text import TfidfVectorizer
from b_prediction import atomic_json, canonical, csvfile, jsonl, safe_path

B = Path(__file__).resolve().parent
LETTERS = {'CTNF','CTFR','CTRS','NOA','CTAV','CTEQ','CTMS','EXIN'}
REPLIES = {'REM','A...','A.NE','A.AF','A.I.','AMSB'}
MAILED = {'MCTNF','MCTFR','MN/=.','MCTRS','MCTAV','MCTEQ','MCTMS','MEXIN'}


def rows(path):
    with Path(path).open(newline='') as f:
        return list(csv.DictReader(f))


def sha1(path):
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def scrub(text):
    # Remove signature/contact lines before any model-facing text is extracted.
    text = re.sub(r'(?im)^.*(?:directed to|whose telephone|examiner[\s,:;]|art unit|supervisor|telephone|e-mail|@).*$',' ',text)
    text = re.sub(r'(?m)^\s*/[^/\n\d]{2,100}/\s*$',' ',text)
    text = re.sub(r'\b\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b',' ',text)
    return re.sub(r'\s+',' ',text).strip()


def pair_candidates(catalog, signatures, cutoff, transactions=()):
    """Conservative provisional pairing, not a replacement for D's adjudication."""
    docs = [d for d in catalog if d['official_date'] < cutoff]
    letters = sorted([d for d in docs if d['doc_code'] in LETTERS],
                     key=lambda d:(d['official_date'],d['doc_id']))
    grouped = defaultdict(list)
    for d in docs:
        if d['doc_code'] in REPLIES:
            grouped[d['official_date']].append(d)
    result = []
    for reply_date, replies in sorted(grouped.items()):
        before = [d for d in letters if d['official_date'] < reply_date]
        after = [d for d in letters if d['official_date'] > reply_date]
        if not before or not after or any(d['official_date']==reply_date for d in letters):
            continue
        previous_date, next_date = before[-1]['official_date'], after[0]['official_date']
        previous = [d for d in before if d['official_date']==previous_date]
        following = [d for d in after if d['official_date']==next_date]
        if len(previous)!=1 or len(following)!=1:
            continue
        previous, following = previous[0], following[0]
        if (previous['doc_code'] not in {'CTNF','CTFR'} or following['doc_code'] not in {'CTNF','CTFR'}
                or signatures.get(previous['doc_id']) is not True or signatures.get(following['doc_id']) is not True):
            continue
        # Do not silently jump across a later applicant reply, unseen mailed action,
        # RCE, abandonment, or revival. D will adjudicate these excluded cases.
        if any(reply_date < other < next_date for other in grouped):
            continue
        blocked = False
        for t in transactions:
            day, code = t['event_date'], t['event_code']
            if reply_date <= day <= next_date:
                if ((code in MAILED and day < next_date) or code.startswith('MABN')
                        or code in {'RCEX','ABN9'} or 'reviv' in t.get('event_description','').lower()):
                    blocked=True
        if blocked:
            continue
        result.append({'prior_doc_id':previous['doc_id'],'prior_date':previous_date,
                       'reply_date':reply_date,'reply_doc_ids':sorted(d['doc_id'] for d in replies),
                       'next_doc_id':following['doc_id'],'next_date':next_date,'next_code':following['doc_code'],
                       'elapsed_days':(date.fromisoformat(next_date)-date.fromisoformat(reply_date)).days})
    return result


def retrieve(query, candidates, k=3):
    """Fit lexical retrieval on eligible pre-outcome context, never action labels."""
    if not candidates:
        return []
    texts=[v['retrieval_text'] for v in candidates]
    try:
        vectorizer=TfidfVectorizer(lowercase=True,stop_words='english',ngram_range=(1,2),
                                   sublinear_tf=True,max_features=40000)
        matrix=vectorizer.fit_transform(texts)
        similarities=(matrix @ vectorizer.transform([query]).T).toarray().ravel()
    except ValueError as exc:
        if 'empty vocabulary' not in str(exc):
            raise
        similarities=[0.]*len(candidates)
    ordering=sorted(range(len(candidates)),key=lambda i:(-similarities[i],candidates[i]['episode_id']))
    selected=[];families=set();apps=set()
    for i in ordering:
        v=candidates[i]
        if v['family_root'] in families or v['app_id'] in apps:
            continue
        selected.append({**v,'similarity':float(similarities[i])})
        families.add(v['family_root']);apps.add(v['app_id'])
        if len(selected)==k:
            break
    return selected


def query_text(dataset, example):
    parts=[]
    target_id=(example.get('target') or {}).get('doc_id')
    for d in example['inputs']:
        if d['date'] > example['cutoff']:
            raise ValueError('Future input in current case')
        if target_id and d['doc_id']==target_id:
            raise ValueError('Target in observed inputs')
        # Use observed OA, reply and claims; no target labels, ODP title/CPC/status.
        if d['doc_code'] not in LETTERS | REPLIES | {'CLM'}:
            continue
        path=safe_path(dataset,d['text_path'])
        if path and path.is_file():
            text=path.read_text()
            if path.suffix=='.json':
                text=' '.join(str(c.get('text','')) for c in json.loads(text))
            parts.append(scrub(text)[:12000])
    query='\n'.join(parts)
    if not query.strip():
        raise ValueError('No observed query text')
    return query


def render_profile(case, setting, selected, encoding, max_tokens=8000):
    if setting not in {'own','mismatched','artunit'}:
        raise ValueError('Unknown profile setting')
    identity = (case['examiner_id'] if setting=='own' else case['control_examiner_id']
                if setting=='mismatched' else 'pooled art unit '+case['control_art_unit'])
    # No model-facing indication of which examiner is a mismatched control.
    header=(f"Examiner history reference (draft; before {case['cutoff']}; other patent families).\n"
            f"Reference: {identity}.\nStatistics: pending D; no examiner rates or outcome priors are supplied.\n"
            'Provisional verified-rejection pairs only; allowance and ambiguous episodes are excluded pending D.\n')
    budget=900
    while True:
        blocks=[]
        for i,v in enumerate(selected,1):
            lines=[encoding.decode(encoding.encode(s,disallowed_special=())[:budget]) for s in v['summary_lines']]
            blocks.append(f"Past case {i} (application {v['app_id']}):\n"+'\n'.join(lines))
        text=header+'\n'.join(blocks)+'\nUse the present case record to decide; these examples are historical references.\n'
        n=len(encoding.encode(text,disallowed_special=()))
        if n<=max_tokens:
            return text,n
        budget//=2
        if budget<8:
            raise ValueError('Token cap too small for provenance header and examples')


def doc_path(corpus, app, doc, substantive=False):
    folder=corpus/'apps'/app/'text'
    suffix='.substantive.txt' if substantive else '.txt'
    paths=sorted(folder.glob('*_'+doc+suffix))
    if len(paths)!=1:
        return None
    return paths[0]


def evidence(path, corpus, max_chars=700):
    """An exact source line range, with sanitized quote for the draft summary."""
    raw=path.read_text();lines=raw.splitlines()
    useful=[]
    for i,line in enumerate(lines,1):
        text=scrub(line)
        if len(text)>35:
            useful.append((i,text))
    favored=[v for v in useful if re.search(r'claim|reject|amend|allowab|cancel|travers|argu|withdraw|response',v[1],re.I)]
    chosen=(favored or useful)[:5]
    if not chosen:
        return None
    start=chosen[0][0];end=chosen[-1][0]
    excerpt=scrub('\n'.join(lines[start-1:end]))[:max_chars]
    return {'path':str(path.relative_to(corpus)),'sha1':sha1(path),'start_line':start,'end_line':end,
            'quote':excerpt,'transform':'contact/signature removal; whitespace normalization; prefix clipping'}


def build_episodes(corpus,index,actions,cutoff_by_examiner):
    signatures=defaultdict(dict)
    for a in actions:
        signatures[(a['examiner_id'],a['app_id'])][a['doc_id']]=(
            True if a['signed_by_this_examiner']=='true' else False if a['signed_by_this_examiner']=='false' else None)
    episodes=[];audit=[]
    for item in index:
        app=item['app_id'];examiner=item['examiner_id'];cutoff=cutoff_by_examiner[examiner]
        cat=rows(corpus/'apps'/app/'catalog.csv');transactions=rows(corpus/'apps'/app/'transactions.csv')
        candidates=pair_candidates(cat,signatures[(examiner,app)],cutoff,transactions)
        accepted=0;missing_reply_n=0
        for pair in candidates:
            prior=doc_path(corpus,app,pair['prior_doc_id'],True) or doc_path(corpus,app,pair['prior_doc_id'])
            following=doc_path(corpus,app,pair['next_doc_id'],True)
            if following is None or not following.read_text().strip():
                following=doc_path(corpus,app,pair['next_doc_id'])
            replies=[doc_path(corpus,app,doc) for doc in pair['reply_doc_ids']]
            replies=[p for p in replies if p and p.read_text().strip()]
            if not prior or not prior.read_text().strip() or not following:
                continue
            prior_ev=evidence(prior,corpus);next_ev=evidence(following,corpus)
            # Prefer substantive REM text over amendment cover forms.
            replies.sort(key=lambda p:('_REM_' not in p.name,p.name))
            if replies:
                reply_ev=evidence(replies[0],corpus)
                reply_line=f"Applicant reply ({pair['reply_date']}): {reply_ev['quote']}" if reply_ev else ''
            else:
                missing_reply_n+=1
                catalog_path=corpus/'apps'/app/'catalog.csv'
                line_numbers=[i+2 for i,d in enumerate(cat) if d['doc_id'] in pair['reply_doc_ids']]
                reply_ev={'path':str(catalog_path.relative_to(corpus)), 'sha1':sha1(catalog_path),
                          'start_line':min(line_numbers),'end_line':max(line_numbers),
                          'quote':'Catalog confirms reply document IDs: '+', '.join(pair['reply_doc_ids']),
                          'date':pair['reply_date'],'transform':'selected catalog rows; reply body unavailable'}
                reply_line=(f"Applicant reply filed {pair['reply_date']}; reply text unavailable in corpus. "
                            'No response strategy or argument is inferred from metadata.')
            if not all([prior_ev,next_ev,reply_ev]):
                continue
            source_paths=[prior,following,*replies]
            if any(p.name[:10]>=cutoff for p in source_paths):
                raise ValueError('Post-cutoff episode source')
            e={**pair,'episode_id':f"{examiner}:{app}:{pair['reply_date']}:{pair['next_doc_id']}",
               'examiner_id':examiner,'app_id':app,'family_root':item['family_root'],
               'evidence':{'prior':prior_ev,'reply':reply_ev,'next':next_ev},
               'source_dates':sorted({p.name[:10] for p in source_paths}|{pair['reply_date']}),
               'source_paths':[str(p.relative_to(corpus)) for p in source_paths],
               'source_sha1':{str(p.relative_to(corpus)):sha1(p) for p in source_paths},
               'reply_text_status':'available' if replies else 'unavailable',
               'summary_lines':[reply_line,
                                f"Next signed examiner action ({pair['next_date']}): {pair['next_code']}, after {pair['elapsed_days']} days. {next_ev['quote']}"],
               'summary_method':'deterministic_extractive_two_lines_v1',
               'episode_status':'B_provisional_not_D_adjudicated',
               'retrieval_text':scrub(prior.read_text())[:6000]+'\n'+'\n'.join(scrub(p.read_text())[:6000] for p in replies)[:12000]}
            episodes.append(e);accepted+=1
        audit.append({'app_id':app,'examiner_id':examiner,'candidate_pairs':len(candidates),'usable_pairs':accepted,
                      'pairs_missing_reply_text':missing_reply_n,
                      'noa_in_catalog_before_latest_cutoff':sum(d['doc_code']=='NOA' and d['official_date']<cutoff for d in cat),
                      'scope':'verified CTNF/CTFR only; NOA and ambiguous pairs pending D'})
    return episodes,audit


def validate_manifest(corpus):
    errors=[];count=0;total=0
    for row in rows(corpus/'MANIFEST.csv'):
        p=safe_path(corpus,row['path']);count+=1
        if not p.is_file() or p.stat().st_size!=int(row['bytes']) or sha1(p)!=row['sha1']:
            errors.append(row['path'])
        if p.is_file():total+=p.stat().st_size
    if errors:
        raise ValueError('Corpus integrity failures: '+str(errors[:10]))
    return {'files_checked':count,'total_bytes':total,'mismatches':errors,'manifest_sha1':sha1(corpus/'MANIFEST.csv')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,default=B.parent/'shared/profile_corpus_v2/profile_corpus')
    parser.add_argument('--dataset',type=Path,default=B.parent/'shared/data_package/data_v1_share')
    parser.add_argument('--output',type=Path,default=B/'outputs/profile_phase1_v2')
    a=parser.parse_args();corpus=a.corpus.resolve();dataset=a.dataset.resolve();out=a.output.resolve()
    if out.exists():
        raise ValueError('Output exists; use a new directory to preserve the previous build')
    os.environ.setdefault('TIKTOKEN_CACHE_DIR',str(B/'outputs/profile_phase1_v2_support/tokenizer'))
    import tiktoken
    encoding=tiktoken.get_encoding('o200k_base')
    integrity=validate_manifest(corpus)
    print('Corpus MANIFEST verified: '+str(integrity['files_checked'])+' files',flush=True)
    cases=rows(corpus/'cases.csv');index=rows(corpus/'profile_index.csv');actions=rows(corpus/'actions.csv')
    assert len(cases)==100 and len({c['sample_id'] for c in cases})==100
    benchmarks=rows(dataset/'sample_manifest.csv');benchmark_apps={r['app_id'] for r in benchmarks}
    benchmark_families={r['family_id'] for r in benchmarks}
    if any(i['family_root'] in benchmark_families or i['app_id'] in benchmark_apps for i in index):
        raise ValueError('Profile family/application overlaps the benchmark')
    cutoff_by_examiner=defaultdict(str);units=defaultdict(set)
    for c in cases:
        for e in [c['examiner_id'],c['control_examiner_id']]:
            cutoff_by_examiner[e]=max(cutoff_by_examiner[e],c['cutoff'])
            units[e].add(c['control_art_unit'])
        if c['examiner_id']==c['control_examiner_id']:
            raise ValueError('Own and control examiner identical')
    episodes,app_audit=build_episodes(corpus,index,actions,cutoff_by_examiner)
    out.mkdir(parents=True)
    jsonl(out/'episodes_B_provisional.jsonl',episodes)
    csvfile(out/'application_pair_audit.csv',app_audit)
    by_examiner=defaultdict(list)
    for e in episodes:
        by_examiner[e['examiner_id']].append(e)
    print('Built '+str(len(episodes))+' provisional signed-rejection episodes',flush=True)
    checks=[];retrieval=[];identity=[];profiles={};query_manifest=[]
    for c in cases:
        app=c['sample_id'];example=json.loads((dataset/'examples'/f'{app}.json').read_text())
        if example['cutoff']!=c['cutoff']:
            raise ValueError('Benchmark/corpus cutoff mismatch: '+app)
        query=query_text(dataset,example)
        query_manifest.append({'sample_id':app,'query_sha1':hashlib.sha1(query.encode()).hexdigest(),
                               'observed_doc_ids':[v['doc_id'] for v in example['inputs']],
                               'cutoff':c['cutoff'],'query_uses_labels':False})
        identity.append({'sample_id':app,'v12_examiner_id':example['examiner_id'],'v2_examiner_id':c['examiner_id'],
                         'changed':example['examiner_id']!=c['examiner_id'],'signer_source':c['signer_source'],
                         'art_unit_v2':c['o1_art_unit'],'control_art_unit':c['control_art_unit']})
        units_exam={ex for ex,us in units.items() if c['control_art_unit'] in us}
        pools={'own':by_examiner[c['examiner_id']], 'mismatched':by_examiner[c['control_examiner_id']],
               'artunit':[e for ex in sorted(units_exam) if ex!=c['examiner_id'] for e in by_examiner[ex]]}
        for setting,pool in pools.items():
            eligible=[e for e in pool if e['next_date']<c['cutoff']
                      and all(day<c['cutoff'] for day in e['source_dates'])
                      and e['family_root'] not in benchmark_families and e['app_id'] not in benchmark_apps]
            selected=retrieve(query,eligible)
            text,token_n=render_profile(c,setting,selected,encoding)
            clean_selected=[{k:v for k,v in e.items() if k!='retrieval_text'} for e in selected]
            profile={'schema_version':'B-phase1-v2-draft-1','sample_id':app,'prediction_sample_id':app+'_t1',
                     'cutoff':c['cutoff'],'setting':setting,'target_examiner_id':c['examiner_id'],
                     'profile_examiner_id':c['examiner_id'] if setting=='own' else c['control_examiner_id'] if setting=='mismatched' else None,
                     'control_art_unit':c['control_art_unit'],'signer_source':c['signer_source'],
                     'statistics':None,'statistics_status':'pending_D_stats_cards',
                     'D_episode_status':'pending_D_episodes; B provisional signed-rejection pairs only',
                     'C_review_status':'pending','freeze_status':'draft_pending_D_and_C','retrieved_cases':clean_selected,
                     'requested_k':3,'retrieved_k':len(selected),'candidate_episode_n':len(eligible),
                     'profile_text':text,'tokenizer':'o200k_base','token_count':token_n,'max_tokens':8000,
                     'retrieval_method':'TF-IDF unigram/bigram cosine; candidates fit per case and setting; pre-outcome texts only',
                     'artunit_pool_scope':'available same-unit own/control examiners; own target excluded; not population representative',
                     'limitations':['NOA signature not verified by A actions.csv; excluded pending D',
                                    'No D statistics or complete episode adjudication yet; do not treat as final shared profile',
                                    'Cutoff/signature/family gates apply before retrieval; no model calls',
                                    'Same-unit control uses A historical-art-unit selection, not art_unit_today metadata']}
            folder=out/'profiles'/setting;folder.mkdir(parents=True,exist_ok=True)
            path=folder/f'{app}.json';atomic_json(path,profile);profile_sha=sha1(path)
            profiles[(app,setting)]={**profile,'file_sha1':profile_sha,'file_path':str(path.relative_to(out))}
            checks.append({'sample_id':app,'setting':setting,'eligible_episodes':len(eligible),'retrieved_k':len(selected),
                           'max_source_date':max((max(e['source_dates']) for e in selected),default=''),
                           'cutoff':c['cutoff'],'all_sources_strictly_before_cutoff':all(day<c['cutoff'] for e in selected for day in e['source_dates']),
                           'no_benchmark_family_overlap':all(e['family_root'] not in benchmark_families for e in selected),
                           'signature_verified':True,'token_count':token_n,'within_8000_tokens':token_n<=8000,
                           'profile_sha1':profile_sha,'profile_path':str(path.relative_to(out)),
                           'ready_for_phase2':False,'reason':'pending_D_statistics_and_episodes_and_C_review'})
            for rank,e in enumerate(selected,1):
                retrieval.append({'sample_id':app,'setting':setting,'rank':rank,'episode_id':e['episode_id'],'app_id':e['app_id'],
                                  'family_root':e['family_root'],'examiner_id':e['examiner_id'],'reply_date':e['reply_date'],
                                  'next_date':e['next_date'],'next_code':e['next_code'],'similarity':e['similarity'],
                                  'zero_similarity':e['similarity']==0,'profile_sha1':profile_sha})
    csvfile(out/'leak_check_B.csv',checks);csvfile(out/'retrieval_audit.csv',retrieval)
    csvfile(out/'examiner_identity_changes.csv',identity);jsonl(out/'query_manifest.jsonl',query_manifest)
    # Stable, coverage-oriented review selection: identity exceptions, repeated-cutoff
    # examiners and low-coverage cases first, then sorted IDs. Review all 3 variants.
    repeated=set(json.loads((corpus/'checks.json').read_text())['examiners_used_with_several_cutoffs'])
    priority=sorted(cases,key=lambda c:(c['signer_source']=='o1_signature',
                                        int(c['n_own_usable_before_cutoff'])>=30,
                                        c['examiner_id'] not in repeated,c['sample_id']))[:10]
    review=[];pages=[]
    review_dir=out/'C_review';review_dir.mkdir()
    for c in priority:
        app=c['sample_id'];sections=[]
        for setting in ['own','mismatched','artunit']:
            p=profiles[(app,setting)]
            section=f'<h2>{html.escape(setting)}</h2><pre>{html.escape(p["profile_text"])}</pre>'
            for e in p['retrieved_cases']:
                section+=f'<h3>{html.escape(e["episode_id"])}</h3>'
                for role,ev in e['evidence'].items():
                    source=corpus/ev['path']
                    section+=f'<p>{role}: {html.escape(ev["path"])} lines {ev["start_line"]}–{ev["end_line"]}; SHA1 {ev["sha1"]}</p><blockquote>{html.escape(ev["quote"])}</blockquote>'
                    full=source.read_text()
                    if source.suffix=='.csv':
                        full='\n'.join(full.splitlines()[ev['start_line']-1:ev['end_line']])
                    section+='<details><summary>Source text for review only (never model input)</summary><pre>'+html.escape(full)+'</pre></details>'
            sections.append(section)
            review.append({'sample_id':app,'setting':setting,'profile_path':p['file_path'],'profile_sha1':p['file_sha1'],
                           'cutoff':c['cutoff'],'dates_ok':'','families_ok':'','identity_ok':'','summary_faithful':'',
                           'rank_relevant':'','statistics_status_ok':'','verdict':'pending','reviewer':'','notes':''})
        page=f'{app}.html';(review_dir/page).write_text('<!doctype html><meta charset="utf-8"><style>body{max-width:1050px;margin:35px auto;font:16px/1.5 sans-serif}pre{white-space:pre-wrap}blockquote{background:#f3f4f6;padding:15px}</style>'+f'<h1>Profile review {app}</h1><p>Cutoff {c["cutoff"]}; {c["signer_source"]}. Draft: D statistics and C approval pending.</p>'+''.join(sections))
        pages.append(f'<li><a href="{page}">{app}</a></li>')
    (review_dir/'index.html').write_text('<!doctype html><meta charset="utf-8"><h1>C review: 10 cases, all 3 variants</h1><ul>'+''.join(pages)+'</ul>')
    csvfile(out/'profile_review_C.csv',review)
    summary={'cases':len(cases),'profiles':len(checks),'variants':['own','mismatched','artunit'],
             'provisional_episodes':len(episodes),'profile_source_applications':len(index),
             'episodes_missing_reply_text':sum(e['reply_text_status']=='unavailable' for e in episodes),
             'profiles_with_k3':sum(c['retrieved_k']==3 for c in checks),'profiles_with_insufficient_k':sum(c['retrieved_k']<3 for c in checks),
             'zero_similarity_retrieved':sum(c['zero_similarity'] for c in retrieval),
             'max_profile_tokens':max(c['token_count'] for c in checks),'tokenizer':'o200k_base',
             'tokenizer_version':tiktoken.__version__,'identity_changes':sum(c['changed'] for c in identity),
             'cutoff_violations':sum(not c['all_sources_strictly_before_cutoff'] for c in checks),
             'family_violations':sum(not c['no_benchmark_family_overlap'] for c in checks),
             'integrity':integrity,'api_calls':0,'status':'B_draft_deliverables_complete_pending_D_and_C',
             'D_dependency':'User confirmed D episodes.csv and stats_cards.jsonl not delivered',
             'C_review_cases':[c['sample_id'] for c in priority],
             'source_sha1':{f:sha1(corpus/f) for f in ['cases.csv','profile_index.csv','actions.csv','checks.json','README.md']},
             'builder_sha1':sha1(Path(__file__))}
    atomic_json(out/'build_summary.json',summary)
    print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__':
    main()
