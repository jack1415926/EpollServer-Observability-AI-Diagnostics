"""Summarize saved evaluation rows without network calls or model-based grading."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics


def mean(values):
    return round(statistics.mean(values), 4) if values else None


def summarize(directory, reviews=None):
    manifest = json.loads((directory/'manifest.json').read_text())
    rows = [json.loads(line) for line in (directory/'results.jsonl').read_text().splitlines()]
    keys = [(r['case_id'],r['kind'],r['mode']) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate_result_rows')
    review_map = {}
    if reviews:
        if reviews['manifest_sha256'] != hashlib.sha256((directory/'manifest.json').read_bytes()).hexdigest():
            raise ValueError('review_manifest_mismatch')
        for review in reviews['items']:
            key = (review['case_id'], review['mode'])
            if key in review_map:
                raise ValueError('duplicate_review')
            review_map[key] = review
    groups = []
    for kind in ('retrieval','diagnosis'):
        for mode in sorted({r['mode'] for r in rows if r['kind']==kind}):
            for split in ('dev','holdout','all'):
                selected = [r for r in rows if r['kind']==kind and r['mode']==mode and
                            (split=='all' or r['split']==split)]
                ok = [r for r in selected if r['status']=='ok']
                group = {'kind':kind,'mode':mode,'split':split,'attempted':len(selected),'ok':len(ok),
                         'seconds_mean':mean([r['seconds'] for r in ok]),
                         'seconds_median':statistics.median([r['seconds'] for r in ok]) if ok else None}
                if kind=='retrieval':
                    for metric in ('recall_at_5','mrr_at_5'):
                        group[metric] = mean([r['metrics'][metric] for r in ok if r['metrics'][metric] is not None])
                else:
                    for check in ('citation_ids_exist','required_evidence_present','expected_conclusion_matches'):
                        values = [r['checks'][check] for r in ok if r['checks'][check] is not None]
                        group[check]={'passed':sum(values),'denominator':len(values)}
                    checked=[]
                    for row in ok:
                        review=review_map.get((row['case_id'],mode))
                        if not review:continue
                        report_hash=hashlib.sha256(json.dumps(row['report'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                        if review['report_sha256']!=report_hash:
                            raise ValueError('review_report_mismatch')
                        facts=row['report']['analysis']['facts']
                        causes=row['report']['analysis']['possible_causes']
                        if len(review['fact_support'])!=len(facts) or len(review['cause_support'])!=len(causes):
                            raise ValueError('review_claim_count_mismatch')
                        if any(s not in ('supported','partial','unsupported','unverifiable') for s in review['fact_support']+review['cause_support']):
                            raise ValueError('invalid_support_label')
                        checked.append(review)
                    group['reviewed']=len(checked)
                    group['fact_support']=dict(Counter(s for review in checked for s in review['fact_support']))
                    group['cause_support']=dict(Counter(s for review in checked for s in review['cause_support']))
                    for field in ('required_points_met','factual_error','time_instance_confusion','appropriate_uncertainty','summary_supported'):
                        values=[review[field] for review in checked]
                        group[field]={'true':sum(values),'denominator':len(values)}
                groups.append(group)
    calls=[call for row in rows for call in row['calls']]
    services={}
    for service in sorted({c['service'] for c in calls}):
        selected=[c for c in calls if c['service']==service]
        usage_keys={key for c in selected for key,value in (c.get('usage') or {}).items() if type(value) in (int,float)}
        services[service]={'requests':len(selected),'errors':sum('error' in c for c in selected),
                           'usage_missing':sum(c.get('usage') is None for c in selected),
                           'usage_sums':{key:sum((c.get('usage') or {}).get(key,0) for c in selected) for key in usage_keys},
                           'returned_models':sorted({c['model'] for c in selected if c.get('model')}),
                           'finish_reasons':dict(Counter(reason for c in selected for reason in c.get('finish_reason',[]) if reason))}
    if review_map and len(review_map)!=sum(g.get('reviewed',0) for g in groups if g['split']=='all'):
        raise ValueError('orphan_review')
    return {'dataset':manifest['dataset'],'started_at':manifest['started_at'],
            'scope':'fixed synthetic tool replay; not live diagnosis accuracy',
            'rows':len(rows),'errors':sum(r['status']!='ok' for r in rows),'groups':groups,
            'services':services,'semantic_review':'assistant rubric review, not independent expert truth' if reviews else 'pending',
            'cache_note':'per-case query/rerank reuse; latency and usage are execution-order dependent'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--reviews',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=summarize(args.directory,json.loads(args.reviews.read_text()) if args.reviews else None)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print('Saved',args.output,'rows',result['rows'],'errors',result['errors'])


if __name__=='__main__':main()
