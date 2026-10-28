"""Validate source-based review bindings and summarize preserved attempts; no model calls."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from statistics import median


def report_hash(report):
    return hashlib.sha256(json.dumps(report,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def summarize(run, reviews):
    rows=[json.loads(line) for line in (run/'results.jsonl').read_text().splitlines()]
    keys=[(r['id'],r['strategy']) for r in rows]
    if len(keys)!=len(set(keys)):raise ValueError('duplicate_attempt_keys')
    review=json.loads(reviews.read_text())
    if review['results_sha256']!=hashlib.sha256((run/'results.jsonl').read_bytes()).hexdigest():
        raise ValueError('review_results_hash_mismatch')
    bindings={(r['id'],r['strategy']):r for r in review['reviews']}
    if len(bindings)!=len(review['reviews']):raise ValueError('duplicate_review_keys')
    expected={(r['id'],r['strategy']) for r in rows if r['status']=='ok'}
    if set(bindings)!=expected:raise ValueError('missing_or_orphan_review')
    for row in rows:
        if row['status']!='ok':continue
        entry=bindings[(row['id'],row['strategy'])]
        if entry['report_sha256']!=report_hash(row['report']):raise ValueError('review_report_hash_mismatch')
        if entry['outcome'] not in ('pass','partial','fail'):raise ValueError('invalid_review_outcome')
        for group in ('facts','possible_causes','excluded_causes'):
            support=entry['claim_support'][group]
            if len(support)!=len(row['report']['analysis'].get(group,[])):
                raise ValueError('review_claim_count_mismatch')
            if any(value not in ('supported','partial','unsupported','unverifiable') for value in support):
                raise ValueError('invalid_claim_support_label')
    groups={}
    for strategy in sorted({r['strategy'] for r in rows}):
        selected=[r for r in rows if r['strategy']==strategy]
        calls=[call for row in selected for call in row['calls']]
        successful=[r for r in selected if r['status']=='ok']
        services={}
        for service in sorted({c['service'] for c in calls}):
            entries=[c for c in calls if c['service']==service]
            usage={}
            for key in ('prompt_tokens','completion_tokens','input_tokens','output_tokens','total_tokens'):
                values=[e['usage'][key] for e in entries if isinstance(e.get('usage'),dict) and key in e['usage']]
                if values:
                    numeric=[v for v in values if isinstance(v,(int,float)) and type(v) is not bool]
                    usage[key]=sum(numeric) if numeric else None
            services[service]={'calls':len(entries),'missing_usage':sum(not e.get('usage') for e in entries),
                               'available_usage_sums':usage}
        groups[strategy]={'attempts':len(selected),'delivered':len(successful),
            'rejected':len(selected)-len(successful), 'semantic_outcomes':dict(Counter(
                bindings[(r['id'],r['strategy'])]['outcome'] for r in successful)),
            'all_attempt_seconds_median':median(r['seconds'] for r in selected),
            'executed_tool_calls':sum(len(r['tools']) for r in selected), 'services':services}
    paired_ids={r['id'] for r in rows if r['strategy']=='targeted'} & {r['id'] for r in rows if r['strategy']=='fixed'}
    paired={}
    for strategy in ('targeted','fixed'):
        selected=[r for r in rows if r['id'] in paired_ids and r['strategy']==strategy]
        if not selected:continue
        calls=[c for r in selected for c in r['calls']]
        total=[c['usage']['total_tokens'] for c in calls if c['service']=='DEEPSEEK' and
               isinstance(c.get('usage'),dict) and isinstance(c['usage'].get('total_tokens'),int)]
        paired[strategy]={'cases':len(selected),'delivered':sum(r['status']=='ok' for r in selected),
            'executed_tool_calls':sum(len(r['tools']) for r in selected),'actual_api_calls':len(calls),
            'available_deepseek_total_tokens':sum(total) if total else None,
            'all_attempt_seconds_median':median(r['seconds'] for r in selected)}
    return {'scope':review['scope'],'reviewer':review['reviewer'],'by_strategy':groups,'paired_cases':sorted(paired_ids),
            'paired':paired,
            'failed_attempts':[{'id':r['id'],'strategy':r['strategy'],'error':r['error']} for r in rows if r['status']!='ok'],
            'warning':'semantic labels are source-based reviewer judgments, not automatic truth; failed attempts stay outside semantic denominators'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--reviews',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('review_summary_exists_use_new_output')
    args.output.write_text(json.dumps(summarize(args.run,args.reviews),ensure_ascii=False,indent=2)+'\n')
