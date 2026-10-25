"""Read-only baseline report and explicit, run-bound provisional review ledger."""
import argparse
from collections import Counter
import json
from pathlib import Path

from .run import SUITE, METHODS, digest, save

DIMENSIONS = ('supported_facts', 'required_facts', 'uncertainty', 'scope', 'safety')


def summarize(run, suite_path=SUITE, reviews=None, corrections=()):
    run = Path(run)
    manifest = json.loads((run/'manifest.json').read_text())
    suite = json.loads(Path(suite_path).read_text())
    if manifest['suite_sha256'] != digest(Path(suite_path).read_bytes()):
        raise ValueError('suite_changed_since_run')
    expected_ids = {c['id'] for c in suite['cases'] if manifest['selection']['split'] in ('all', c['split'])}
    if manifest['selection']['ids']:
        expected_ids &= set(manifest['selection']['ids'].split(','))
    cases = {c['id']: c for c in suite['cases'] if c['id'] in expected_ids}
    retrievals = [json.loads(p.read_text()) for p in sorted((run/'retrieval').glob('*.json'))]
    generations = [json.loads(p.read_text()) for p in sorted((run/'generation').glob('*.json'))]
    origins = {(r['case_id'], r['mode']): manifest['run_id'] for r in generations}
    source_runs = [manifest['run_id']]
    for directory in corrections:
        directory = Path(directory)
        corrected = json.loads((directory/'manifest.json').read_text())
        for key in ('suite_sha256', 'index_sha256', 'prompt_sha256', 'models'):
            if corrected[key] != manifest[key]:
                raise ValueError('correction_changes_frozen_product_or_questions')
        if corrected.get('working_source_documents') != manifest.get('working_source_documents'):
            raise ValueError('correction_changes_working_source_freshness')
        if any(corrected['source_hashes'][name] != value for name, value in manifest['source_hashes'].items()
               if name.startswith('diagnosis/')):
            raise ValueError('correction_changes_production_code')
        before = json.loads((run/'observable-fixtures.json').read_text())
        after = json.loads((directory/'observable-fixtures.json').read_text())
        if digest(before) != manifest['fixture_sha256'] or digest(after) != corrected['fixture_sha256']:
            raise ValueError('invalid_frozen_fixture_archive')
        changed = {key for key in before if before[key] != after[key]}
        rows = [json.loads(p.read_text()) for p in sorted((directory/'generation').glob('*.json'))]
        if {(r['case_id'], r['mode']) for r in rows} != {(key, mode) for key in changed for mode in ('rag', 'runtime_only')}:
            raise ValueError('correction_must_retest_exactly_changed_fixtures')
        generations = [r for r in generations if r['case_id'] not in changed]+rows
        origins.update({(r['case_id'], r['mode']): corrected['run_id'] for r in rows})
        source_runs.append(corrected['run_id'])
    if any(r['case_id'] not in cases for r in retrievals+generations):
        raise ValueError('results_contain_unselected_case')
    if len({r['case_id'] for r in retrievals}) != len(retrievals):
        raise ValueError('duplicate_retrieval_result')
    pairs = {(r['case_id'], r['mode']) for r in generations}
    if len(pairs) != len(generations) or any(mode not in ('rag', 'runtime_only') for _, mode in pairs):
        raise ValueError('invalid_generation_result')
    result = {'run_id': digest(source_runs) if corrections else manifest['run_id'],
              'source_runs': source_runs, 'evidence_kind': manifest['input_kind'],
              'usage_scope': 'Included final result rows only; probes/superseded/replaced runs are separate artifacts.',
              'retrieval': {}, 'generation': {}, 'usage': {}, 'provisional_review': {}}
    for split in ('development', 'holdout', 'all'):
        selected = {key for key, c in cases.items() if split in ('all', c['split'])}
        for method in METHODS:
            values = [r['methods'][method] for r in retrievals if r['case_id'] in selected and
                      r['status'] == 'succeeded' and method in r['methods']]
            relevant = [r for r in values if r['recall_at_5'] is not None]
            if values:
                result['retrieval'][split+'/'+method] = {'cases': len(values), 'gold_cases': len(relevant),
                    'recall_at_5': sum(r['recall_at_5'] for r in relevant)/len(relevant) if relevant else None,
                    'mrr_at_5': sum(r['reciprocal_rank'] for r in relevant)/len(relevant) if relevant else None,
                    'no_gold_answer_returned': sum(r.get('returned_despite_no_gold_answer', False) for r in values)}
        for mode in ('rag', 'runtime_only'):
            values = [r for r in generations if r['case_id'] in selected and r['mode'] == mode]
            if values:
                result['generation'][split+'/'+mode] = {
                    'attempted': len(values), 'succeeded': sum(r['status'] == 'succeeded' for r in values),
                    'failures': [{'case_id': r['case_id'], 'error_type': r.get('error_type'),
                                  'validation_errors': r.get('validation_errors', [])}
                                 for r in values if r['status'] != 'succeeded'],
                    'schema_citation_checks_are_semantic_accuracy': False}
    all_traces = [t for row in retrievals+generations for t in row.get('trace', [])]
    for service in ('EMBEDDING', 'RERANK', 'DEEPSEEK'):
        trace = [t for t in all_traces if t['service'] == service]
        actual = [t for t in trace if not t.get('cache_hit')]
        usage = [t['usage'] for t in actual if t.get('usage')]
        result['usage'][service] = {'actual_requests': len(actual), 'cache_hits': len(trace)-len(actual),
            'reported_total_tokens': sum(u['total_tokens'] for u in usage) if usage else None,
            'responses_with_usage': len(usage),
            'length_finishes': sum(t.get('finish_reason') == 'length' for t in actual)}
    successful = {(r['case_id'], r['mode']): r for r in generations if r['status'] == 'succeeded'}
    review_rows = []
    if reviews:
        ledger = json.loads(Path(reviews).read_text())
        if ledger['run_id'] != result['run_id']:
            raise ValueError('reviews_belong_to_different_run')
        seen = set()
        for row in ledger['reviews']:
            key = (row['case_id'], row['mode'])
            if key in seen or key not in successful:
                raise ValueError('invalid_or_duplicate_review')
            if row.get('source_run_id') != origins[key]:
                raise ValueError('review_uses_superseded_case_output')
            seen.add(key)
            if any(row.get(d) not in ('pass', 'partial', 'fail', 'not_assessed') for d in DIMENSIONS):
                raise ValueError('invalid_review_dimension')
            if not row.get('note'):
                raise ValueError('review_requires_evidence_note')
            review_rows.append(row)
        result['provisional_review']['reviewer'] = ledger['reviewer']
        result['provisional_review']['independent_expert_validated'] = ledger.get('independent_expert_validated', False)
    result['provisional_review'].update(reviewed=len(review_rows), pending=len(successful)-len(review_rows),
        semantic_accuracy=None, explanation='Internal rubric review is provisional; no expert accuracy claim.')
    for mode in ('rag', 'runtime_only'):
        rows = [r for r in review_rows if r['mode'] == mode]
        result['provisional_review'][mode] = {d: dict(Counter(r[d] for r in rows)) for d in DIMENSIONS}
    expected_generation = len(cases)*2 if manifest['diagnosis_enabled'] else 0
    result['completion'] = {'expected_retrieval_cases': len(cases), 'retrieval_cases': len(retrievals),
        'expected_generation_runs': expected_generation, 'generation_runs': len(generations),
        'complete': len(retrievals) == len(cases) and len(generations) == expected_generation}
    return result


def markdown(value):
    lines = ['# 第一批诊断质量基线结果', '',
             f"冻结 run：`{value['run_id']}`。输入：`{value['evidence_kind']}`。", '',
             '这是内部案例与锚点标注下的基线，不是生产性能测试或外部专家确认的诊断准确率。', '',
             '| 集合 / 检索方式 | 金标案例数 | Recall@5 | MRR@5 |', '|---|---:|---:|---:|']
    for name, row in value['retrieval'].items():
        recall = f"{row['recall_at_5']:.4f}" if row['recall_at_5'] is not None else '—'
        mrr = f"{row['mrr_at_5']:.4f}" if row['mrr_at_5'] is not None else '—'
        lines.append(f"| {name} | {row['gold_cases']} | {recall} | {mrr} |")
    lines += ['', '| 集合 / 诊断方式 | 运行数 | 成功生成 | 失败案例 |', '|---|---:|---:|---|']
    for name, row in value['generation'].items():
        lines.append(f"| {name} | {row['attempted']} | {row['succeeded']} | {', '.join(r['case_id'] for r in row['failures']) or '—'} |")
    review = value['provisional_review']
    lines += ['', f"语义复核：已复核 {review['reviewed']} 条，待复核 {review['pending']} 条。",
              '生成成功只表示结构和引用编号校验通过；semantic_accuracy 保持 null。', '',
              '详细请求用量、失败字段和复核维度计数见同目录 baseline-report.json。', '',
              '检索缓存命中不计作新API请求；耗时为合成工具回放条件，不代表真实在线延迟。',
              '无金标答案的案例不参与Recall/MRR，返回候选的现象单独记录。', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--suite', type=Path, default=SUITE)
    parser.add_argument('--reviews', type=Path)
    parser.add_argument('--correction', type=Path, action='append', default=[],
                        help='combine a verified replay-only correction with exactly the changed cases rerun')
    args = parser.parse_args()
    value = summarize(args.run, args.suite, args.reviews, args.correction)
    save(args.run/'baseline-report.json', value)
    (args.run/'baseline-report.md').write_text(markdown(value))
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
