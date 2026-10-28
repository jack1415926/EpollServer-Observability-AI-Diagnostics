"""Locate frozen v1 anchor misses without API calls, retuning or changing labels."""
import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np

from .evaluate import DATA, digest, read
from .retrieval import INDEX


def classify(group, trace, chunks):
    def hits(ids):
        return any(chunks[key]['path']==span['path'] and
                   chunks[key]['start_line']<=span['line']<=chunks[key]['end_line']
                   for key in ids for span in group)
    if hits(trace['returned']):return 'returned'
    if hits(trace['candidates']):return 'rerank_dropped'
    if hits(trace['bm25']+trace['dense']):return 'fusion_dropped'
    return 'not_in_union40'


def audit(directory, index):
    manifest=read(directory/'manifest.json')
    if digest(index)!=manifest['index_sha256'] or digest(DATA/'labels.json')!=manifest['data']['labels']:
        raise ValueError('audit_inputs_differ_from_frozen_run')
    with np.load(index,allow_pickle=False) as bundle:
        chunks={c['id']:c for c in json.loads(str(bundle['metadata']))}
    labels=read(DATA/'labels.json')
    cases=[];counts=Counter()
    for row in map(json.loads,(directory/'results.jsonl').read_text().splitlines()):
        if row['kind']!='retrieval' or row['mode']!='hybrid_rerank':continue
        if row['status']!='ok':raise ValueError('cannot_audit_failed_retrieval')
        trace=row['trace'][0]
        states=[classify(group,trace,chunks) for group in labels[row['case_id']]['relevant_groups']]
        counts.update(states)
        cases.append({'case_id':row['case_id'],'split':row['split'],
                      'groups':[{'anchors':group,'stage':state} for group,state in zip(labels[row['case_id']]['relevant_groups'],states)],
                      'recall_at_5':row['metrics']['recall_at_5'],
                      'returned':[{'path':c['path'],'start_line':c['start_line'],'end_line':c['end_line']} for c in row['results']]})
    if len({c['case_id'] for c in cases})!=len(cases):raise ValueError('duplicate_case')
    return {'scope':'original labeled anchors only; not semantic relevance adjudication',
            'manifest_sha256':digest(directory/'manifest.json'),'index_sha256':digest(index),
            'case_count':len(cases),'evidence_group_count':sum(counts.values()),'stages':dict(counts),
            'macro_recall_at_5':sum(c['recall_at_5'] for c in cases)/len(cases) if cases else None,
            'cases':cases}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--index',type=Path,default=INDEX)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=audit(args.directory,args.index)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print('Cases',result['case_count'],'groups',result['evidence_group_count'],'stages',result['stages'])
