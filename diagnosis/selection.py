"""Local minimum contracts for interpreting selected evidence; no additional runtime reads."""
import hashlib
import json
from pathlib import Path
import re

from .config import ROOT
from .providers import ProviderError

REGISTRY = Path(__file__).with_name('observation_contracts.json')
PLANNER_VERSION = 'minimum-evidence-v1'


def observation_only(question, route):
    return route.intent == 'runtime' and not re.search(
        r'为什么|原因|能否|能不能|断定|区分|故障|异常|重启|崩溃|业务健康', question) and bool(re.search(
        r'(?:连接数|队列|有效Worker).{0,12}(?:多少|为几)|监控是否存活', question, re.I))


def contract_topics(question, route, evidence):
    sources = {e['source'] for e in evidence}
    explain = bool(re.search(r'为什么|原因|能否|是否|能不能|意味着|断定|判断|解释|区分|故障|异常|重启|补起|dead|stale|unavailable|mismatch', question, re.I))
    topics = set()
    statuses = set()
    for e in evidence:
        if e['source'] == 'workers':
            try:
                data = json.loads(e['excerpt']).get('response', {}).get('data') or {}
                statuses.update(w.get('status') for w in data.get('workers', []))
            except (ValueError, TypeError, AttributeError): pass
    if observation_only(question, route) and not statuses - {'fresh'}:
        return []
    if 'workers' in sources and (explain or statuses - {'fresh'}):
        topics.update(('sampling', 'worker_states'))
    if 'health' in sources and explain:
        topics.update(('sampling', 'capabilities'))
    if 'events' in sources and explain:
        topics.add('events')
    if route.intent == 'runtime' and re.search(r'重启|补起|拉起|退出|崩溃|SIG|dead|存活', question, re.I):
        topics.add('worker_policy')
    if route.intent == 'runtime' and re.search(r'CPU|内存|MySQL|Redis|日志|业务健康|TCP健康|命中率', question, re.I) and explain:
        topics.add('capabilities')
    if route.intent == 'experiments' and (route.experiment_action == 'compare' or explain):
        topics.add('experiments')
    return sorted(topics)


def contracts(topics):
    raw = REGISTRY.read_bytes()
    registry = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    values = []
    lines = raw.decode().splitlines()
    for topic in topics:
        if topic not in registry['topics']:
            raise ValueError('unknown_contract_topic')
        item = registry['topics'][topic]
        for ref in item['references']:
            path = ROOT / ref['path']
            if (path.is_symlink() or not path.is_file() or
                    hashlib.sha256(path.read_bytes()).hexdigest() != ref['sha256']):
                raise ProviderError('contract_source_changed_review_required')
        line = next(i for i, text in enumerate(lines, 1) if text.strip() == '"'+topic+'": {')
        values.append({'path': 'diagnosis/observation_contracts.json', 'heading': item['title'],
            'start_line': line, 'end_line': line+len(json.dumps(item, ensure_ascii=False, indent=2).splitlines())-1,
            'sha256': digest, 'text': item['text'], 'scope': 'project_document', 'source_changed': False,
            'contract_topic': topic, 'references': item['references'],
            'corpus': {'layer': 'current_contract', 'source_quality': 'reviewed_code_contract',
                       'policy_version': registry['version'], 'superseded_by': []}})
    return values
