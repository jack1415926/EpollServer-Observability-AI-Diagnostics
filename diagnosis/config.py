"""Local configuration; exported variables take precedence over the ignored file."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_environment():
    path = ROOT / '.env.stage5'
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.startswith('#'):
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())


def redact(value):
    import re
    import json
    def clean(text):
        if text.lstrip().startswith(('{', '[')):
            try:
                nested = json.loads(text)
            except (ValueError, TypeError):
                pass
            else:
                return json.dumps(walk(nested), ensure_ascii=False)
        for key, secret in os.environ.items():
            if any(word in key for word in ('API_KEY', 'PASSWORD', 'TOKEN')) and len(secret) >= 6:
                text = text.replace(secret, '[REDACTED]')
        text = re.sub(r'sk-[A-Za-z0-9_-]{10,}', '[REDACTED]', text)
        assignment = r'''(?i)(["']?\b(?:password|passwd|api[_ -]?key|secret|token)\b["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[REDACTED\]|[^\s,;"'{}\[\]]+)'''
        def replace(match):
            quote = match[2][0] if match[2][0] in ('"', "'") else ''
            return match[1] + quote + '[REDACTED]' + quote
        return re.sub(assignment, replace, text)

    def walk(item):
        if isinstance(item, str):
            return clean(item)
        if isinstance(item, list):
            return [walk(part) for part in item]
        if isinstance(item, dict):
            return {key: '[REDACTED]' if re.fullmatch(r'(?i)password|passwd|api[_ -]?key|secret|token', key)
                    else walk(part) for key, part in item.items()}
        return item

    # Redact values before serializing so a matched secret cannot consume JSON punctuation.
    return clean(value) if isinstance(value, str) else json.dumps(walk(value), ensure_ascii=False)
