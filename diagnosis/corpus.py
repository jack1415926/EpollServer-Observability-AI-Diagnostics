"""Reviewed source/section annotations and bounded corpus selection."""
import hashlib
import json
from pathlib import Path
import re

from .config import ROOT

POLICY = Path(__file__).with_name('corpus_policy.json')
LAYERS = {'current_contract', 'historical_case', 'technical_principle', 'planning_progress'}


def policy_digest():
    return hashlib.sha256(POLICY.read_bytes()).hexdigest()


def load_policy(root=ROOT):
    policy = json.loads(POLICY.read_text())
    titles = {}
    for path, source in policy['sources'].items():
        file = root / path
        if (not file.is_file() or file.is_symlink() or
                hashlib.sha256(file.read_bytes()).hexdigest() != source['reviewed_sha256']):
            raise ValueError('corpus_source_changed_review_annotations: ' + path)
        if source['layer'] not in LAYERS or any(
                value.get('layer', source['layer']) not in LAYERS for value in source['sections'].values()):
            raise ValueError('invalid_corpus_layer')
        titles[path] = {title for _, _, title in markdown_headings(file.read_text())}
        if not set(source['sections']) <= titles[path]:
            raise ValueError('corpus_section_missing: ' + path)
        end = 0
        for span in source.get('spans', []):
            if not end < span['start_line'] <= span['end_line'] <= len(file.read_text().splitlines()):
                raise ValueError('invalid_corpus_annotation_span')
            if span.get('layer', source['layer']) not in LAYERS:
                raise ValueError('invalid_corpus_layer')
            end = span['end_line']
    for source in policy['sources'].values():
        for section in [*source['sections'].values(), *source.get('spans', [])]:
            for target in section.get('superseded_by', []):
                if target['heading'] not in titles.get(target['path'], set()):
                    raise ValueError('corpus_supersession_target_missing')
    return policy


def markdown_headings(raw):
    fence = None
    for number, line in enumerate(raw.splitlines(), 1):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        match = re.match(r'^(#{1,4})\s+(.+)', line) if fence is None else None
        if match:
            yield number, len(match[1]), line.lstrip('# ')
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None


def annotate(policy, path, headings, line):
    source = policy['sources'][path]
    result = {key: source[key] for key in
              ('layer', 'published_on', 'applicable_version', 'components', 'source_quality')}
    result.update({key: source[key] for key in ('source_url', 'source_version', 'source_published_on',
                   'source_snapshot_sha256', 'acquired_on', 'summary_author') if key in source})
    result['superseded_by'] = []
    for heading in headings:
        if heading in source['sections']:
            result.update({'superseded_by': [], **source['sections'][heading]})
        date = re.search(r'20\d{2}-\d{2}-\d{2}', heading)
        if date:
            result['published_on'] = date[0]
    for span in source.get('spans', []):
        if span['start_line'] <= line <= span['end_line']:
            result.update({k: v for k, v in span.items() if k not in ('start_line', 'end_line')})
    result['policy_version'] = policy['version']
    result['reviewed_on'] = policy['reviewed_on']
    result['section_path'] = headings or [Path(path).stem]
    return result


def search_purpose(query):
    # History broadens eligibility; it never converts an old source into a current contract.
    historical = r'历史|当时|上游|曾经|旧版|早期|路线|规划|计划|复盘|变更|演进|验收记录|\b(history|historical|previous|upstream|roadmap|legacy)\b'
    return 'history' if re.search(historical, query, re.I) else 'default'


def eligible(chunk, purpose):
    metadata = chunk['corpus']
    return purpose == 'history' or (metadata['layer'] != 'planning_progress' and
                                    not metadata['superseded_by'])
