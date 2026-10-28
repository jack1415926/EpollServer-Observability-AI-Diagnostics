"""Bounded remote calls with no implicit retries or credential-bearing errors."""
import json
import math
import os
import time
from urllib.parse import urlsplit

import httpx
import numpy as np


class ProviderError(RuntimeError):
    pass


class RerankOrder(list):
    def __init__(self, results):
        super().__init__(item['index'] for item in results)
        self.scores = {item['index']: item['relevance_score'] for item in results}


class Providers:
    def __init__(self, client):
        self.client = client
        self.model = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')
        self.telemetry = []

    async def post(self, service, path, payload):
        defaults = {'DEEPSEEK': 'https://api.deepseek.com',
                    'EMBEDDING': 'https://api.siliconflow.cn/v1',
                    'RERANK': 'https://api.siliconflow.cn/v1'}
        key = os.getenv(service + '_API_KEY', '')
        if not key:
            raise ProviderError(service.lower() + '_unconfigured')
        base = os.getenv(service + '_BASE_URL', defaults[service]).rstrip('/')
        if not base.startswith('https://'):
            raise ProviderError(service.lower() + '_requires_https')
        if service == 'DEEPSEEK' and path == '/responses':
            base = base.removesuffix('/v1').removesuffix('/beta')
        elif service == 'DEEPSEEK' and any(t.get('function', {}).get('strict') for t in payload.get('tools', [])):
            base = base.removesuffix('/v1')
            if not base.endswith('/beta'):
                base += '/beta'
        entry = {'service': service, 'requested_model': payload['model'], 'model': None,
                 'usage': None, 'finish_reason': None, 'response_status': None,
                 'origin': urlsplit(base).netloc.rsplit('@',1)[-1], 'api_path': path}
        started = time.monotonic()
        try:
            async with self.client.stream('POST', base + path, json=payload,
                    headers={'Authorization': 'Bearer ' + key}, timeout=30) as response:
                if response.status_code != 200:
                    raise ProviderError(f'{service.lower()}_http_{response.status_code}')
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > 16 * 1024 * 1024:
                        raise ProviderError(service.lower() + '_response_too_large')
                data = json.loads(body)
                if isinstance(data, dict):
                    entry.update(model=data.get('model'), usage=data.get('usage'), response_status=data.get('status'),
                        finish_reason=[choice.get('finish_reason') for choice in data.get('choices', [])] or None,
                        incomplete_details=data.get('incomplete_details'))
                return data
        except (httpx.HTTPError, ValueError):
            entry['error'] = service.lower() + '_invalid_or_unavailable'
            raise ProviderError(service.lower() + '_invalid_or_unavailable') from None
        except ProviderError as exc:
            entry['error'] = str(exc)
            raise
        finally:
            entry['seconds'] = round(time.monotonic()-started, 4)
            self.telemetry.append(entry)

    async def embed(self, texts):
        data = await self.post('EMBEDDING', '/embeddings', {
            'model': os.getenv('EMBEDDING_MODEL', 'Qwen/Qwen3-Embedding-8B'), 'input': texts})
        try:
            items = sorted(data['data'], key=lambda item: item['index'])
            if [item['index'] for item in items] != list(range(len(texts))):
                raise ValueError()
            vectors = np.asarray([item['embedding'] for item in items], dtype=np.float32)
            if vectors.ndim != 2 or not np.isfinite(vectors).all():
                raise ValueError()
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if (norms <= 0).any():
                raise ValueError()
            return vectors / norms
        except (KeyError, TypeError, ValueError):
            raise ProviderError('embedding_invalid_response') from None

    async def rerank(self, query, documents):
        data = await self.post('RERANK', '/rerank', {
            'model': os.getenv('RERANK_MODEL', 'Qwen/Qwen3-Reranker-8B'),
            'query': query, 'documents': documents, 'top_n': min(5, len(documents)),
            'return_documents': False})
        try:
            result = data['results']
            indices = [item['index'] for item in result]
            if (not result or len(set(indices)) != len(indices) or
                    any(type(i) is not int or i < 0 or i >= len(documents) for i in indices) or
                    any(not math.isfinite(item['relevance_score']) for item in result)):
                raise ValueError()
            return RerankOrder(result)
        except (KeyError, TypeError, ValueError):
            raise ProviderError('rerank_invalid_response') from None

    async def chat(self, messages, tools=None, tool_choice=None):
        payload = {'model': self.model, 'messages': messages, 'max_tokens': 4000,
                   'thinking': {'type': 'disabled'}, 'stream': False}
        if tools:
            payload['tools'] = tools
            if tool_choice is not None:
                payload['tool_choice'] = tool_choice
        else:
            payload['response_format'] = {'type': 'json_object'}
        data = await self.post('DEEPSEEK', '/chat/completions', payload)
        try:
            if data['choices'][0].get('finish_reason') == 'length':
                raise ProviderError('deepseek_output_truncated')
            message = data['choices'][0]['message']
            if not isinstance(message, dict):
                raise ValueError()
            return message
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError('deepseek_invalid_response') from None

    async def structured(self, messages, schema):
        # Preserve native call/result pairing when moving the final report to Responses.
        inputs, calls, outputs = [], set(), set()
        for message in messages:
            if message['role'] == 'tool':
                call_id = message['tool_call_id']
                if call_id not in calls or call_id in outputs:
                    raise ProviderError('deepseek_invalid_message_history')
                outputs.add(call_id)
                inputs.append({'type': 'function_call_output', 'call_id': call_id, 'output': message['content']})
            else:
                if message.get('content'):
                    inputs.append({'role': message['role'], 'content': message['content']})
                for call in message.get('tool_calls', []):
                    if not call['id'] or call['id'] in calls:
                        raise ProviderError('deepseek_invalid_message_history')
                    calls.add(call['id'])
                    inputs.append({'type': 'function_call', 'call_id': call['id'],
                                   'name': call['function']['name'], 'arguments': call['function']['arguments']})
        if calls != outputs:
            raise ProviderError('deepseek_invalid_message_history')
        data = await self.post('DEEPSEEK', '/responses', {
            'model': self.model, 'input': inputs, 'reasoning': {'effort': 'none'},
            'max_output_tokens': 4000, 'stream': False,
            'text': {'format': {'type': 'json_schema', 'name': 'diagnosis_report', 'schema': schema}}})
        if not isinstance(data, dict):
            raise ProviderError('deepseek_invalid_structured_response')
        if data.get('status') == 'incomplete':
            reason = (data.get('incomplete_details') or {}).get('reason')
            raise ProviderError('deepseek_output_truncated' if reason == 'max_output_tokens' else 'deepseek_incomplete_response')
        if data.get('status') != 'completed' or data.get('error'):
            raise ProviderError('deepseek_failed_response')
        try:
            texts = []
            for item in data['output']:
                if item['type'] == 'reasoning':
                    continue
                if (item['type'] != 'message' or item['role'] != 'assistant' or
                        item.get('status', 'completed') != 'completed'):
                    raise ValueError()
                for part in item['content']:
                    if part['type'] != 'output_text' or not isinstance(part['text'], str):
                        raise ValueError()
                    texts.append(part['text'])
            text = ''.join(texts)
            if not text.strip():
                raise ValueError()
            return text
        except (KeyError, TypeError, ValueError):
            raise ProviderError('deepseek_invalid_structured_response') from None
