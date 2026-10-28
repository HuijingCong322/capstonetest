"""B prediction: isolated preparation, resumable Azure calls, and scoring.

No credentials are saved. Gold files are consumed only by the score command.
"""
from __future__ import annotations
import argparse
import csv
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import uuid
from urllib.parse import urlparse

TYPES = ('CTNF', 'CTFR', 'NOA')
TYPE_MAP = {'nonfinal_rejection':'CTNF', 'final_rejection':'CTFR', 'allowance':'NOA'}
GROUNDS = {'101','102','103','112a','112b','112','dp'}
DEV = {'11968167','11968173','11968176'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def index(rows, key='sample_id'):
    result = {}
    for row in rows:
        value = row.get(key)
        if not value or value in result:
            raise ValueError(f'Missing/duplicate {key}: {value}')
        result[value] = row
    return result


def flag(value):
    return value is True or str(value).lower() in ('true','1')


def safe_path(root, relative):
    if not relative:
        return None
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f'Input path escapes dataset: {relative}')
    return candidate


def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+'\n')
    temporary.replace(path)


def jsonl(path, rows):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(''.join(canonical(row)+'\n' for row in rows))
    temporary.replace(path)


def csvfile(path, rows, fields=None):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or (list(rows[0]) if rows else ['sample_id'])
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


def render_text(path):
    if path.suffix == '.json':
        claims = json.loads(path.read_text())
        if not isinstance(claims, list):
            raise ValueError('Claim listing JSON must be a list')
        return '\n'.join(f"{c['number']}. {c['text']}" for c in claims)
    return path.read_text()


def prepare(dataset, states, state_inputs, output, group='core'):
    dataset, output = Path(dataset), Path(output)
    with (dataset/'sample_manifest.csv').open() as stream:
        manifest = list(csv.DictReader(stream))
    index(manifest)
    selected = [r for r in manifest if (flag(r['test_core']) if group == 'core' else r['app_id'] in DEV)]
    if not selected:
        raise ValueError('No selected samples')
    state_index = index([r for r in read_jsonl(states) if r['stage']=='A1_cutoff'])
    source_index = index([r for r in read_jsonl(state_inputs) if r['stage']=='A1_cutoff'])
    inputs, coverage, types, grounds = [], [], [], []
    for row in selected:
        sample = row['sample_id']; cutoff = row['cutoff']; date.fromisoformat(cutoff)
        example_path = safe_path(dataset, 'examples/'+row['app_id']+'.json')
        example = json.loads(example_path.read_text())
        if example['cutoff'] != cutoff or example['app_id'] != row['app_id']:
            raise ValueError('Manifest/example identity mismatch: '+sample)
        label = example['labels']; target = example.get('target') or {}
        target_path = safe_path(dataset, target.get('text_path'))
        target_available = bool(target_path and target_path.is_file())
        mapped = TYPE_MAP.get(label['next_type'])
        eligible = bool(mapped and not flag(label['ordering_uncertain']) and not flag(label['censored']))
        if eligible and label['occurrence'] != int(mapped != 'NOA'):
            raise ValueError('Type/occurrence gold conflict: '+sample)
        types.append({'sample_id':sample, 'app_id':row['app_id'], 'next_type':label['next_type'],
                      'occurrence':label['occurrence'], 'eligible':eligible,
                      'excluded_reason':'' if eligible else 'type_out_of_scope_or_uncertain_or_censored',
                      'target_doc_id':target.get('doc_id'), 'target_text_path':target.get('text_path'),
                      'target_text_available':target_available, 'review_status':'automatic_unreviewed'})
        parsed = (label.get('grounds_o2') or {}).get('pairs')
        grounds.append({'sample_id':sample, 'pairs':None if parsed is None else sorted({(int(a),b) for a,b in parsed if b != 'objection'}),
                        'objection_pairs':None if parsed is None else sorted({(int(a),b) for a,b in parsed if b == 'objection'}),
                        'target_doc_id':target.get('doc_id'), 'target_text_path':target.get('text_path'),
                        'review_status':'automatic_unreviewed', 'ground_label_status':'unknown' if parsed is None else 'parsed'})
        parts, doc_ids, missing = [], [], []
        for doc in example['inputs']:
            if date.fromisoformat(doc['date']) > date.fromisoformat(cutoff):
                raise ValueError('Future input document: '+sample+'/'+doc['doc_id'])
            if target.get('doc_id') and doc['doc_id'] == target['doc_id']:
                raise ValueError('Target document included in history: '+sample)
            path = safe_path(dataset, doc['text_path'])
            if not path or not path.is_file():
                missing.append(doc['doc_id']); continue
            if target_path and path == target_path:
                raise ValueError('Target path included in history: '+sample)
            text = render_text(path).strip()
            if not text:
                missing.append(doc['doc_id']); continue
            doc_ids.append(doc['doc_id'])
            parts.append(f"=== DOC {doc['doc_id']} | {doc['doc_code']} | {doc['date']} | role={doc['role']} | source={doc['source']} ===\n{text}")
        if set(doc_ids+missing) != set(row['input_doc_ids'].split(';')):
            raise ValueError('Manifest/history document mismatch: '+sample)
        state, source = state_index.get(sample), source_index.get(sample)
        reasons = []
        if state is None or source is None:
            reasons.append('missing_A1_state_or_state_input')
        else:
            for value in (state, source):
                if value['app_id'] != row['app_id'] or value['cutoff'] != cutoff:
                    raise ValueError('State identity/cutoff mismatch: '+sample)
            expected_hash = hashlib.sha1(source['input_text'].encode()).hexdigest()[:16]
            if source['input_hash'] != expected_hash or state['input_hash'] != expected_hash:
                raise ValueError('State/input hash mismatch: '+sample)
            if not set(source['doc_ids']).issubset(set(doc_ids)):
                raise ValueError('State used documents outside current history: '+sample)
            if not state.get('parse_ok') or state.get('finish_reason') != 'stop' or not isinstance(state.get('state'),dict):
                reasons.append('invalid_or_incomplete_state')
        if not parts:
            reasons.append('no_readable_history')
        required_a1 = {doc['doc_id'] for doc in example['inputs'] if doc['role']=='A1'}
        if not required_a1 or not required_a1.issubset(set(doc_ids)):
            reasons.append('missing_actual_A1')
        coverage.append({'sample_id':sample,'app_id':row['app_id'],'input_hash':row['input_hash'],
                         'status':'blocked' if reasons else 'ready','reason':';'.join(reasons),
                         'missing_doc_ids':';'.join(sorted(set(missing+row.get('missing_doc_ids','').split(';'))- {''})),
                         'type_eligible':eligible})
        if reasons:
            continue
        history = '\n\n'.join(parts)
        message = (f"Application {row['app_id']}. A1 cutoff: {cutoff}. Predict the next examiner document.\n"
                   f"DOCUMENTS (observed history):\n{history}\n\nPREDICTED A1 STATE (may contain errors):\n{canonical(state['state'])}")
        inputs.append({'sample_id':sample,'app_id':row['app_id'],'cutoff':cutoff,'group':group,
                       'input_hash':row['input_hash'],'state_input_hash':state['input_hash'],
                       'state_hash':digest(state['state']),'state_prompt_hash':state.get('prompt_hash'),
                       'state_omitted_doc_ids':source.get('omitted_doc_ids',[]),
                       'input_doc_ids':doc_ids,'missing_doc_ids':coverage[-1]['missing_doc_ids'],
                       'user_message':message})
    jsonl(output/'inputs'/f'prediction_{group}.jsonl',inputs)
    csvfile(output/f'preparation_coverage_{group}.csv',coverage)
    csvfile(output/'gold'/f'action_type_gold.{group}.draft.csv',types)
    jsonl(output/'gold'/f'claim_ground_gold.{group}.draft.jsonl',grounds)
    summary = {'group':group,'selected_n':len(selected),'ready_n':len(inputs),'blocked_n':len(selected)-len(inputs),
               'type_eligible_n':sum(r['eligible'] for r in types),'gold_status':'automatic_unreviewed',
               'missing_history_n':sum(bool(r['missing_doc_ids']) for r in coverage),
               'unknown_ground_labels_n':sum(r['pairs'] is None for r in grounds)}
    atomic_json(output/f'preparation_{group}.json',summary)
    return summary


def probability(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1:
        raise ValueError('Invalid probability')
    return float(value)


def validate_prediction(value):
    if not isinstance(value,dict):
        raise ValueError('Prediction must be an object')
    try:
        p = probability(value['p_second_oa']); pt = value['p_type']
        if not isinstance(pt,dict) or set(pt)!=set(TYPES):
            raise ValueError('p_type must contain exactly CTNF/CTFR/NOA')
        pt = {t:probability(pt[t]) for t in TYPES}
        if abs(sum(pt.values())-1)>1e-6 or abs(p-pt['CTNF']-pt['CTFR'])>1e-6:
            raise ValueError('Inconsistent probabilities')
        winner = max(TYPES,key=pt.get)
        if value['predicted_type'] != winner:
            raise ValueError('Type must be probability argmax (tie CTNF, CTFR, NOA)')
        pairs = value['claim_ground_pairs']
        if not isinstance(pairs,list):
            raise ValueError('Pairs must be a list')
        cleaned = set()
        for pair in pairs:
            if not isinstance(pair,list) or len(pair)!=2:
                raise ValueError('Invalid claim-ground pair')
            claim, ground = pair
            if isinstance(claim,bool) or not isinstance(claim,int) or claim<=0 or ground not in GROUNDS:
                raise ValueError('Invalid claim or ground')
            cleaned.add((claim,ground))
        if winner=='NOA' and cleaned:
            raise ValueError('NOA prediction must have no substantive rejection pairs')
        return {'p_second_oa':p,'p_type':pt,'predicted_type':winner,'claim_ground_pairs':[list(x) for x in sorted(cleaned)]}
    except (KeyError,TypeError) as exc:
        raise ValueError('Malformed prediction fields') from exc


def score(gold, results):
    import sklearn
    from sklearn.metrics import roc_auc_score, accuracy_score, f1_score, brier_score_loss, confusion_matrix
    gi, ri = index(gold), index(results)
    extra = set(ri)-set(gi)
    if extra:
        raise ValueError('Unexpected prediction IDs: '+','.join(sorted(extra)))
    normalized = {}
    for sample,row in ri.items():
        if row['status']=='success':
            normalized[sample] = validate_prediction(row['prediction'])
    eligible = [k for k,r in gi.items() if flag(r['eligible'])]
    scored = [k for k in eligible if k in normalized]
    failed = [k for k in eligible if k not in normalized]
    def gold_status(ids, field):
        statuses = {gi[k].get(field, 'automatic_unreviewed') for k in ids}
        return next(iter(statuses)) if len(statuses)==1 else ('mixed' if statuses else 'no_eligible_gold')
    counts = {'eligible_n':len(eligible),'scored_n':len(scored),'failed_n':len(failed),
              'failed_ids':failed,'excluded_ids':[k for k in gi if k not in eligible],
              'coverage_status':'complete' if not failed else 'partial',
              'gold_status':gold_status(eligible, 'review_status')}
    y = [int(gi[k]['occurrence']) for k in scored]
    probabilities = [normalized[k]['p_second_oa'] for k in scored]
    types = [TYPE_MAP.get(gi[k]['next_type'],gi[k]['next_type']) for k in scored]
    if any(t not in TYPES for t in types) or any(v not in (0,1) for v in y):
        raise ValueError('Invalid eligible gold labels')
    if any(int(t!='NOA')!=v for t,v in zip(types,y)):
        raise ValueError('Gold occurrence/type conflict')
    predicted_types = [normalized[k]['predicted_type'] for k in scored]
    occ = dict(counts, value=float(roc_auc_score(y,probabilities)) if len(set(y))==2 else None,
               reason='' if len(set(y))==2 else 'both_classes_required',
               positive_n=sum(y),negative_n=len(y)-sum(y))
    typ = dict(counts, value=float(f1_score(types,predicted_types,labels=list(TYPES),average='macro',zero_division=0)) if scored else None,
               labels=list(TYPES),confusion_matrix=confusion_matrix(types,predicted_types,labels=list(TYPES)).tolist() if scored else [],
               support={t:types.count(t) for t in TYPES})
    aux = {} if not scored else {'Accuracy':float(accuracy_score(y,[int(p>=.5) for p in probabilities])),
                                 'positive_F1':float(f1_score(y,[int(p>=.5) for p in probabilities],zero_division=0)),
                                 'Brier':float(brier_score_loss(y,probabilities))}
    ground_ids = [k for k in eligible if gi[k].get('pairs') is not None]
    ground_scored = [k for k in ground_ids if k in normalized]
    tp=fp=fn=0
    for k in ground_scored:
        true = set()
        for claim,ground in gi[k]['pairs']:
            if isinstance(claim,bool) or not isinstance(claim,int) or claim<=0 or ground not in GROUNDS:
                raise ValueError('Invalid ground gold: '+k)
            true.add((claim,ground))
        predicted = {tuple(x) for x in normalized[k]['claim_ground_pairs']}
        tp+=len(true&predicted); fp+=len(predicted-true); fn+=len(true-predicted)
    denom=2*tp+fp+fn
    grnd={'eligible_n':len(ground_ids),'scored_n':len(ground_scored),'failed_n':len(ground_ids)-len(ground_scored),
          'unknown_gold_ids':[k for k in eligible if gi[k].get('pairs') is None],
          'coverage_status':'complete' if len(ground_ids)==len(ground_scored) else 'partial',
          'tp':tp,'fp':fp,'fn':fn,'precision':tp/(tp+fp) if tp+fp else None,
          'recall':tp/(tp+fn) if tp+fn else None,'value':2*tp/denom if denom else None,
          'reason':'' if denom else 'no_positive_pairs_or_predictions', 'gold_status':gold_status(ground_ids, 'ground_review_status')}
    return {'Occ':occ,'Type':typ,'Grnd':grnd,'auxiliary':aux,'scoring_library':{'scikit-learn':sklearn.__version__},
            'failure_reasons':{k:ri.get(k,{}).get('reason','missing_output') for k in failed}}


def azure_transport(config):
    import httpx
    endpoint=config['endpoint'].rstrip('/')+'/'
    parsed=urlparse(endpoint)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.path.endswith('/openai/v1/'):
        raise ValueError('Use an HTTPS Azure /openai/v1/ base URL without credentials/query')
    key=os.environ.get('AZURE_OPENAI_API_KEY')
    if not key:
        raise ValueError('AZURE_OPENAI_API_KEY is absent')
    def send(payload):
        with httpx.Client(timeout=300) as client:
            response=client.post(endpoint+'chat/completions',headers={'api-key':key},json=payload)
            response.raise_for_status()
            return response.json()
    return send


def run(inputs, prompt, output, config, transport=None, validator=validate_prediction, task='prediction'):
    rows=read_jsonl(inputs);index(rows)
    output=Path(output);prompt_text=Path(prompt).read_text();prompt_hash=hashlib.sha256(prompt_text.encode()).hexdigest()
    if not config.get('endpoint') or not config.get('deployment'):
        raise ValueError('Azure endpoint/deployment are required')
    send=transport or azure_transport(config)
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]
    config={**{k:config[k] for k in ('endpoint','deployment')},
            'expected_model':config.get('expected_model','gpt-5-mini-2025-08-07'),
            'deployment_type':config.get('deployment_type','Global Standard')}
    if not config['expected_model'].startswith('gpt-5-mini-') or config['deployment_type']!='Global Standard':
        raise ValueError('This setting requires a pinned gpt-5-mini version and declared Global Standard deployment')
    configuration={**config,'task':task,'reasoning_effort':'low','initial_max_completion_tokens':8192,'retry_max_completion_tokens':16384,
                   'content_attempt_limit':2,'temperature':'omitted','transport_retries':0,'response_format':{'type':'json_object'},
                   'implementation_hash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    results=[]
    for row in rows:
        messages=[{'role':'system','content':prompt_text},{'role':'user','content':row['user_message']}]
        request_hash=digest({'messages':messages,'config':configuration,'provenance':{k:v for k,v in row.items() if k!='user_message'}})
        cache=output/'cache'/(request_hash+'.json')
        if cache.is_file():
            cached=json.loads(cache.read_text())
            if cached.get('request_hash')==request_hash and cached.get('status')=='success' and cached.get('model')==config['expected_model']:
                validator(cached['prediction']);results.append(cached)
                print(f"[{len(results)}/{len(rows)}] {row['sample_id']} cached", flush=True)
                continue
        base={k:row.get(k) for k in ('sample_id','app_id','cutoff','input_hash','state_input_hash','state_hash','state_prompt_hash','missing_doc_ids')}
        base['base_user_message_hash']=row.get('base_user_message_hash') or hashlib.sha256(row['user_message'].encode()).hexdigest()
        base['input_setting']=row.get('input_setting','no_profile')
        base['profile_hash']=row.get('profile_hash')
        base.update(run_id=run_id,prompt_hash=prompt_hash,request_hash=request_hash,configuration=configuration)
        budget=8192;final=None
        for attempt in (1,2):
            payload={'model':config['deployment'],'messages':messages,'max_completion_tokens':budget,
                     'reasoning_effort':'low','response_format':{'type':'json_object'}}
            meta={**base,'attempt':attempt,'max_completion_tokens':budget}
            try:
                response=send(payload)
            except Exception as exc:
                code=getattr(getattr(exc,'response',None),'status_code',None)
                meta.update(status='failed',reason='api_error',exception_type=type(exc).__name__,http_status=code)
                atomic_json(output/'raw'/f'{request_hash}_{run_id}_{attempt}.json',meta)
                log_path=output/'usage_log.jsonl';log_path.parent.mkdir(parents=True,exist_ok=True)
                with log_path.open('a') as stream:stream.write(canonical(meta)+'\n')
                final={**meta,'prediction':None};break
            meta['response']=response
            try:
                if not isinstance(response,dict):
                    raise ValueError('Malformed API envelope')
                meta.update(model=response.get('model'),usage=response.get('usage'))
                if meta['model']!=config['expected_model']:
                    raise ValueError('unexpected_model_version')
                choice=response['choices'][0];finish=choice['finish_reason'];content=choice['message']['content']
                meta['finish_reason']=finish
                if finish=='length':
                    raise ValueError('truncated')
                if finish!='stop':
                    raise ValueError('non_stop_finish')
                parsed=validator(json.loads(content))
                meta.update(status='success',reason='')
                final={k:v for k,v in meta.items() if k!='response'}
                final['prediction']=parsed
            except (ValueError,KeyError,TypeError,IndexError):
                reason=('unexpected_model_version' if isinstance(response,dict) and response.get('model')!=config['expected_model']
                        else 'truncated' if meta.get('finish_reason')=='length' else 'invalid_json_or_schema_or_finish')
                meta.update(status='failed',reason=reason)
                final={k:v for k,v in meta.items() if k!='response'}
                final['prediction']=None
            atomic_json(output/'raw'/f'{request_hash}_{run_id}_{attempt}.json',meta)
            log={k:v for k,v in meta.items() if k!='response'}
            log_path=output/'usage_log.jsonl';log_path.parent.mkdir(parents=True,exist_ok=True)
            with log_path.open('a') as stream:stream.write(canonical(log)+'\n')
            if final['status']=='success':
                atomic_json(cache,final);break
            if final['reason']=='unexpected_model_version':
                break
            if attempt==1:
                budget=16384 if meta.get('finish_reason')=='length' else 8192
        results.append(final)
        print(f"[{len(results)}/{len(rows)}] {row['sample_id']} {final['status']} {final['reason']}", flush=True)
        jsonl(output/'examiner_prediction.jsonl',results)
    jsonl(output/'examiner_prediction.jsonl',results)
    csvfile(output/'examiner_prediction.csv',[
        {**{k:r.get(k) for k in ('sample_id','status','reason','input_hash','prompt_hash','request_hash','model')},
         'p_second_oa':(r.get('prediction') or {}).get('p_second_oa'),
         **{'p_'+t:(r.get('prediction') or {}).get('p_type',{}).get(t) for t in TYPES},
         'predicted_type':(r.get('prediction') or {}).get('predicted_type')} for r in results])
    jsonl(output/'claim_ground_predictions.jsonl',[
        {'sample_id':r['sample_id'],'status':r['status'],'claim_ground_pairs':(r.get('prediction') or {}).get('claim_ground_pairs'),
         'input_hash':r['input_hash'],'prompt_hash':r['prompt_hash'],'request_hash':r['request_hash']} for r in results])
    summary={'run_id':run_id,'eligible_n':len(rows),'success_n':sum(r['status']=='success' for r in results),
             'failed_n':sum(r['status']!='success' for r in results),'configuration':configuration}
    atomic_json(output/'run_summary.json',summary)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('prepare')
    for name in ('dataset','states','state-inputs','output'):p.add_argument('--'+name,required=True,type=Path)
    p.add_argument('--group',choices=('core','dev'),default='core')
    p=sub.add_parser('run')
    for name in ('inputs','prompt','output'):p.add_argument('--'+name,required=True,type=Path)
    p=sub.add_parser('score')
    for name in ('types','grounds','predictions','output'):p.add_argument('--'+name,required=True,type=Path)
    args=parser.parse_args()
    if args.command=='prepare':
        result=prepare(args.dataset,args.states,args.state_inputs,args.output,args.group)
    elif args.command=='run':
        result=run(args.inputs,args.prompt,args.output,{'endpoint':os.environ.get('AZURE_OPENAI_ENDPOINT'),
                   'deployment':os.environ.get('AZURE_DEPLOYMENT')})
    else:
        with args.types.open() as stream:types=list(csv.DictReader(stream))
        grounds=index(read_jsonl(args.grounds));index(types)
        if set(grounds)!=set(r['sample_id'] for r in types):raise ValueError('Type/ground IDs differ')
        gold=[{**r,'pairs':grounds[r['sample_id']]['pairs'],
               'ground_review_status':grounds[r['sample_id']].get('review_status','automatic_unreviewed'),
               'occurrence':int(r['occurrence']) if r['occurrence']!='' else None} for r in types]
        result=score(gold,read_jsonl(args.predictions));atomic_json(args.output,result)
    print(json.dumps(result,indent=2))

if __name__=='__main__':
    main()
