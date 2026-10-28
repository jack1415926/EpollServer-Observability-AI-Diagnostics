"""Explicit catalog/legacy corpus compatibility for the retained October 25 baseline."""
import hashlib
import json
import re
from pathlib import Path
from .config import ROOT, redact
CATALOG = Path("diagnosis/knowledge/catalog.json")

def documents(root=ROOT, corpus='current'):
    root = Path(root)
    provenance = {}
    if corpus == 'legacy':
        paths = [root / 'README.md', root / 'docs/DECISIONS.md', root / 'docs/DEVELOPMENT_ISSUES.md']
        paths += sorted((root / 'docs').glob('STAGE_*.md'))
        paths += sorted((root / 'docs/upstream').glob('*.md'))
    elif corpus == 'current':
        catalog_bytes = (root / CATALOG).read_bytes()
        catalog = json.loads(catalog_bytes)
        if catalog.get('schema_version') != 1:
            raise ValueError('invalid_knowledge_catalog')
        paths = []
        for entry in catalog['entries']:
            relative = Path(entry['path'])
            path = root / relative
            if (relative.is_absolute() or '..' in relative.parts or relative.suffix != '.md' or
                    not path.resolve().is_relative_to((root / 'diagnosis/knowledge').resolve()) or
                    entry['kind'] not in ('current_contract', 'verified_incident', 'technical_reference') or
                    not entry['revision'] or not entry['authority'] or not entry['applies_to'] or
                    not path.is_file() or path.is_symlink() or entry['path'] in provenance):
                raise ValueError('invalid_knowledge_entry')
            local = {}
            for source in entry['sources']:
                if source.startswith('https://'):
                    continue
                ref = Path(source)
                if ref.is_absolute() or '..' in ref.parts or not (root/ref).is_file():
                    raise ValueError('invalid_knowledge_supporting_source')
                local[source] = hashlib.sha256((root/ref).read_bytes()).hexdigest()
            provenance[entry['path']] = {**entry, 'catalog_revision': catalog['revision'],
                                         'catalog_sha256': hashlib.sha256(catalog_bytes).hexdigest(),
                                         'local_source_hashes': local}
            paths.append(path)
    else:
        raise ValueError('invalid_knowledge_corpus')
    chunks = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        raw = path.read_text(encoding='utf-8')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        lines = raw.splitlines()
        start, heading, section = 1, path.stem, []

        def emit():
            # Line-based windows preserve a verifiable source span; oversized lines are bounded.
            for offset in range(0, len(section), 18):
                span = section[offset:offset+24]
                content = redact('\n'.join(span))[:3500]
                if not content.strip():
                    continue
                a, b = start + offset, start + offset + len(span) - 1
                key = hashlib.sha256(f'{relative}:{digest}:{a}:{b}'.encode()).hexdigest()[:20]
                metadata = provenance.get(relative)
                chunks.append({'id': key, 'path': relative, 'start_line': a, 'end_line': b,
                    'sha256': digest, 'heading': heading, 'text': content,
                    'scope': metadata['kind'] if metadata else
                        'historical_reference' if '/upstream/' in relative else 'project_document',
                    **({'provenance': metadata} if metadata else {})})

        for number, line in enumerate(lines, 1):
            if re.match(r'^#{1,4}\s', line):
                emit()
                start, heading, section = number, line.lstrip('# '), []
            section.append(line)
        emit()
    return chunks
