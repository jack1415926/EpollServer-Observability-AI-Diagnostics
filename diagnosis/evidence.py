"""Verifiable citations and narrow domain contradictions, not a semantic truth oracle."""
import json
import math
import re
from decimal import Decimal, DecimalException, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, model_validator

BASIS = Literal['observation', 'document', 'historical', 'user_statement', 'unverified_log']


class Support(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, json_schema_extra={
        'required': ['evidence_id', 'pointer', 'value', 'quote', 'operation'],
        'oneOf': [
            {'properties': {'pointer': {'pattern': '^/'}, 'quote': {'const': ''}, 'operation': {'const': 'value'}}},
            {'properties': {'pointer': {'pattern': '^/'}, 'quote': {'const': ''}, 'operation': {'const': 'length'},
                            'value': {'type': 'integer', 'minimum': 0}}},
            {'properties': {'pointer': {'const': ''}, 'quote': {'pattern': r'\S', 'minLength': 1},
                            'operation': {'const': 'value'}, 'value': {'const': None}}}
        ]})
    evidence_id: str = Field(min_length=1, max_length=20)
    pointer: str = Field(max_length=1000)
    value: str | StrictInt | StrictFloat | StrictBool | None
    quote: str = Field(max_length=2000)
    operation: Literal['value', 'length'] = 'value'

    @model_validator(mode='after')
    def one_locator(self):
        if self.quote and not self.quote.strip():
            raise ValueError('one_support_locator_required')
        if bool(self.pointer) == bool(self.quote.strip()) or (self.quote and (
                self.value is not None or self.operation != 'value')):
            raise ValueError('one_support_locator_required')
        if self.operation == 'length' and (type(self.value) is not int or self.value < 0):
            raise ValueError('length_requires_nonnegative_integer')
        return self


class ReportSupportError(ValueError):
    def __init__(self, issues):
        super().__init__('report_support_invalid')
        self.issues = issues


def evidence_basis(item):
    if item['source'] == 'user_statement': return 'user_statement'
    if item['scope'] == 'unverified_log_history': return 'unverified_log'
    if item['scope'] == 'live_observation': return 'observation'
    if item['source'] == 'technical_docs':
        try:
            value = json.loads(item['excerpt'])
            corpus = value.get('corpus', {})
            if value.get('source_changed'):
                return 'historical'
        except (ValueError, AttributeError):
            corpus = {}
        if corpus.get('layer') in ('historical_case', 'planning_progress') or corpus.get('superseded_by'):
            return 'historical'
        return 'historical' if item['scope'] == 'historical_reference' else 'document'
    return 'historical'


def resolve_pointer(value, pointer):
    if not pointer.startswith('/'):
        raise ValueError('invalid_json_pointer')
    ancestors = []
    for token in pointer[1:].split('/'):
        if re.search(r'~(?![01])', token):
            raise ValueError('invalid_json_pointer')
        token = token.replace('~1', '/').replace('~0', '~')
        ancestors.append(value)
        if isinstance(value, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', token): raise ValueError('invalid_json_pointer')
            value = value[int(token)]
        elif isinstance(value, dict): value = value[token]
        else: raise ValueError('invalid_json_pointer')
    return value, ancestors


def support_locators(value, limit=64):
    pending = [('', value)]
    result = []
    while pending and len(result) < limit:
        pointer, node = pending.pop()
        if isinstance(node, (list, dict)):
            if pointer and isinstance(node, list):
                result.append({'pointer': pointer, 'operation': 'length', 'value': len(node)})
            items = enumerate(node) if isinstance(node, list) else node.items()
            children = [(pointer+'/'+str(key).replace('~','~0').replace('/','~1'), child) for key, child in items]
            pending.extend(reversed(children))
        elif pointer and (not isinstance(node, str) or len(node) <= 300):
            result.append({'pointer': pointer, 'operation': 'value', 'value': node})
    return result[:limit]


def same_scalar(actual, expected):
    if isinstance(actual, (dict, list)):
        return False
    if type(actual) is bool or type(expected) is bool:
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return (not isinstance(actual, float) or math.isfinite(actual)) and (
            not isinstance(expected, float) or math.isfinite(expected)) and actual == expected
    return type(actual) is type(expected) and actual == expected


def numbers(text):
    return re.findall(r'(?<![A-Za-z_0-9.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?',
                      re.sub(r'\bE\d+\b', '', str(text)))


def identifiers(text):
    return set(re.findall(r'(?<![A-Za-z0-9])(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{64})(?![A-Za-z0-9])', str(text), re.I))


def numeric_supported(token, values):
    number = Decimal(token)
    for value in values:
        actual = Decimal(value)
        if number == actual: return True
        if '.' in token and '.' in value:
            with localcontext() as context:
                context.prec = max(28, len(token)+len(value)+4)
                try:
                    if number == actual.quantize(Decimal(1).scaleb(number.as_tuple().exponent)):
                        return True
                except DecimalException:
                    continue
    return False


def positive_clauses(text, pattern):
    for part in re.split(r'[。；;\n]|但是|然而|但', text):
        match = re.search(pattern, part, re.I)
        if match and not re.search(r'不|非|未|没有|无需|无法|不能', part[max(0, match.start()-8):match.end()]):
            yield part


def quote_matches(item, quote):
    if quote in item['excerpt']:
        return True
    if item['truncated']:
        return False
    try:
        pending = [json.loads(item['excerpt'])]
    except ValueError:
        return False
    while pending:
        value = pending.pop()
        if isinstance(value, str) and quote in value:
            return True
        if isinstance(value, dict): pending.extend(value.values())
        elif isinstance(value, list): pending.extend(value)
    return False


def support_categories(item, support):
    base = evidence_basis(item)
    allowed = {base}
    pointer = support.get('pointer', '')
    if item['source'] == 'events' and pointer.startswith('/response/data/detection/'):
        return {'observation'}
    # Query availability/counts describe this read, not when the stored records happened.
    if item['source'] in ('events', 'experiments', 'experiments/compare') and (
            pointer in ('/http_status', '/response/code', '/response/validity', '/response/data_valid', '/response/timestamp_unix_ms') or
            pointer.startswith('/response/data/detection/') or
            (support.get('operation') == 'length' and pointer in ('/response/data/events', '/response/data/experiments'))):
        allowed.add('observation')
    if item['scope'] == 'unverified_log_history' and pointer in ('/available', '/instance_verified', '/error', '/reason'):
        allowed.add('observation')
    return allowed


def data_claim_text(claim, by_id):
    text = claim['text']
    # A verified document's stage number is a source label, not a measured metric.
    for item in (by_id[key] for key in claim['evidence_ids'] if key in by_id):
        if item['source'] != 'technical_docs': continue
        try: source = json.loads(item['excerpt'])
        except ValueError: continue
        for number in re.findall(r'阶段\s*(\d+)', text):
            if re.search(r'STAGE_'+re.escape(number)+r'(?:_|\.)', source.get('path','')) or re.search(
                    r'阶段\s*'+re.escape(number)+r'(?!\d)', source.get('text','')):
                text = re.sub(r'阶段\s*'+re.escape(number)+r'(?!\d)', '阶段', text)
        for name, section in re.findall(r'([A-Za-z_][A-Za-z_0-9]*)\((\d+)\)', text):
            label=name+'('+section+')'
            url=source.get('corpus',{}).get('source_url','')
            if label.lower() in source.get('text','').lower() or '/man'+section+'/'+name.lower()+'.'+section in url.lower():
                text=text.replace(label,name)
    return text


def validate_report(analysis, evidence):
    by_id = {e['id']: e for e in evidence}
    issues, checked = [], 0
    groups = ('facts', 'possible_causes', 'excluded_causes')
    for group in groups:
        for index, claim in enumerate(analysis.get(group, [])):
            location = [group, index]
            if not set(claim['evidence_ids']) <= by_id.keys():
                raise ValueError('fabricated_evidence_reference')
            supports = claim.get('support', [])
            if not supports:
                issues.append({'field': location+['support'], 'type': 'support_required'})
                continue
            if set(claim['evidence_ids']) != {s['evidence_id'] for s in supports}:
                issues.append({'field': location+['evidence_ids'], 'type': 'support_reference_mismatch'})
            allowed_numbers, allowed_identifiers, anchors, categories = [], set(), set(), set()
            for offset, support in enumerate(supports):
                where = location+['support', offset]
                item = by_id.get(support['evidence_id'])
                if item is None:
                    issues.append({'field': where, 'type': 'unknown_support_reference'}); continue
                if item['scope'] == 'unavailable':
                    issues.append({'field': where, 'type': 'unavailable_evidence_not_support'}); continue
                options = support_categories(item, support)
                categories.update({claim['basis']} if claim.get('basis') in options else options)
                if support['quote']:
                    if not quote_matches(item, support['quote']):
                        issues.append({'field': where+['quote'], 'type': 'quote_not_in_excerpt'}); continue
                    allowed_numbers += numbers(support['quote'])
                    allowed_identifiers.update(identifiers(support['quote']))
                else:
                    if item['truncated']:
                        issues.append({'field': where, 'type': 'truncated_json_not_resolvable'}); continue
                    try:
                        actual, parents = resolve_pointer(json.loads(item['excerpt']), support['pointer'])
                        if support.get('operation', 'value') == 'length':
                            if not isinstance(actual, (list, dict)) or type(support['value']) is not int:
                                raise ValueError('pointer_value_mismatch')
                            actual = len(actual)
                        if not same_scalar(actual, support['value']):
                            raise ValueError('pointer_value_mismatch')
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        issues.append({'field': where, 'type': 'pointer_value_mismatch' if str(exc) == 'pointer_value_mismatch'
                                       else 'invalid_json_pointer'}); continue
                    allowed_numbers += numbers(actual) if actual is not None else []
                    allowed_identifiers.update(identifiers(actual))
                    worker = re.search(r'(/workers/\d+)(?:/|$)', support['pointer'])
                    if worker:
                        anchors.add((item['id'], support['pointer'][:worker.end(1)]))
                    for parent in parents:
                        if isinstance(parent, dict):
                            scalars=' '.join(str(value) for value in parent.values()
                                if value is not None and type(value) is not bool and not isinstance(value, (dict, list)))
                            allowed_numbers += numbers(scalars)
                            allowed_identifiers.update(identifiers(scalars))
                    field = support['pointer'].rsplit('/', 1)[-1]
                    aliases = {'connections_current':'连接数|connections_current',
                        'receive_queue_messages':'接收队列|收队列|receive_queue_messages',
                        'send_queue_messages':'发送队列|发队列|send_queue_messages', 'age_ms':'样本年龄|age_ms',
                        'cpu_percent':'CPU(?:使用率|占用率)?|cpu_percent', 'rss_bytes':'RSS|常驻内存|rss_bytes'}
                    if actual is None and field in aliases and any(positive_clauses(claim['text'],
                            r'(?:'+aliases[field]+r')[^，,。；]{0,10}(?:为|是|=|：|:)\s*(?:0|零)(?!\d)')):
                        issues.append({'field': location+['text'], 'type': 'null_reported_as_zero'})
                checked += 1
            basis = claim.get('basis')
            if basis not in ('observation', 'document', 'historical', 'user_statement', 'unverified_log') or not categories:
                issues.append({'field': location+['basis'], 'type': 'basis_required'})
            elif basis not in categories or (group == 'facts' and not categories <= {basis, 'document'}):
                issues.append({'field': location+['basis'], 'type': 'evidence_scope_mismatch'})
            if len(anchors) > 1:
                issues.append({'field': location+['support'], 'type': 'mixed_worker_entities'})
            if any(not numeric_supported(token, allowed_numbers) for token in numbers(data_claim_text(claim, by_id))):
                issues.append({'field': location+['text'], 'type': 'numeric_literal_not_supported'})
            if not identifiers(claim['text']) <= allowed_identifiers:
                issues.append({'field': location+['text'], 'type': 'identifier_not_supported'})
    # These reject specific known contradictions; unrecognised language still requires review.
    statuses = set()
    for item in evidence:
        if item['source'] == 'workers':
            try:
                value = json.loads(item['excerpt'])['response']['data'] or {}
                statuses.update(w.get('status') for w in value.get('workers', []))
            except (ValueError, KeyError, TypeError, AttributeError): pass
    if any(item['source']=='workers' for item in evidence):
        for index, claim in enumerate(analysis.get('possible_causes', [])):
            text = claim['text']
            # Narrow known contradictions only; mixed Worker states need entity-level review.
            if statuses and statuses <= {'stale', 'snapshot_identity_mismatch'} and any(
                    positive_clauses(text, r'崩溃|退出|回收')):
                issues.append({'field': ['possible_causes', index, 'text'], 'type': 'postcheck_lifecycle_belongs_in_unknowns'})
            if statuses == {'dead'} and any(positive_clauses(text, r'(?:只是|单纯).{0,12}(?:SIGSTOP|暂停)')):
                issues.append({'field': ['possible_causes', index, 'text'], 'type': 'stop_is_not_dead'})
        texts = [(['summary'], analysis['summary'])]
        texts += [([group, i, 'text'], c['text']) for group in groups for i, c in enumerate(analysis.get(group, []))
                  if c.get('basis') != 'user_statement']
        for field, text in texts:
            if field == ['summary'] or (len(field)>1 and isinstance(field[1],int) and
                    analysis.get(field[0],[])[field[1]].get('basis')=='observation'):
                if re.search(r'overview\s*(?:接口)?\s*(?:返回|响应|显示)',text,re.I):
                    issues.append({'field':field,'type':'endpoint_not_observed'})
            if any(positive_clauses(text, r'(?:快照|采样|发布|名单|采集).{0,25}(?:共享内存|心跳)|(?:共享内存|心跳).{0,20}(?:快照|采样|记录|发布|采集|刷新)')):
                issues.append({'field': field, 'type': 'unsupported_snapshot_mechanism'})
            if 'dead' in statuses and any(positive_clauses(text,
                    r'(?:SIGSTOP|暂停).{0,40}(?:导致|造成|判为|判定为).{0,12}(?:dead|死亡|退出)')):
                issues.append({'field': field, 'type': 'stop_is_not_dead'})
            if 'process_unavailable' in statuses and any(positive_clauses(
                    text.replace('无法读取','读取障碍').replace('不可读','读取障碍').replace('无法解析','解析障碍'),
                    r'(?:SIGSTOP|暂停).{0,90}(?:导致|造成|而).{0,20}(?:process_unavailable|读取障碍|解析障碍)')):
                issues.append({'field':field,'type':'stop_does_not_make_proc_unreadable'})
            if any(positive_clauses(text, r'(?:会|将|能够|可以|已|会被).{0,4}(?:自动重启|自动补起|重新fork)')):
                issues.append({'field': field, 'type': 'automatic_restart_not_supported'})
    claim_numbers = [token for group in groups for c in analysis.get(group, []) for token in numbers(c['text'])]
    if any(not numeric_supported(token, claim_numbers) for token in numbers(analysis['summary'])):
        issues.append({'field': ['summary'], 'type': 'summary_numeric_not_in_claims'})
    claim_identifiers=set().union(*(identifiers(c['text']) for group in groups for c in analysis.get(group, [])))
    if not identifiers(analysis['summary']) <= claim_identifiers:
        issues.append({'field': ['summary'], 'type': 'summary_identifier_not_in_claims'})
    if issues:
        raise ReportSupportError(issues[:30])
    return {'checked_supports': checked, 'checked_claims': sum(len(analysis.get(g, [])) for g in groups),
            'method': 'typed_json_pointer_or_exact_quote_and_scoped_rules', 'semantic_status': 'requires_review'}
