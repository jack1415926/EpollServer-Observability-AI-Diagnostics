"""Bounded remote calls with no implicit retries or credential-bearing errors."""
import json
import math
import os

import httpx
import numpy as np


class ProviderError(RuntimeError):
    pass


class Providers:
    def __init__(self, client):
        self.client = client
        self.model = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')

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
                return json.loads(body)
        except (httpx.HTTPError, ValueError):
            raise ProviderError(service.lower() + '_invalid_or_unavailable') from None

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
            return indices
        except (KeyError, TypeError, ValueError):
            raise ProviderError('rerank_invalid_response') from None

    async def chat(self, messages, tools=None):
        payload = {'model': self.model, 'messages': messages, 'max_tokens': 4000,
                   'thinking': {'type': 'disabled'}, 'stream': False}
        if tools:
            payload['tools'] = tools
        else:
            payload['response_format'] = {'type': 'json_object'}
        data = await self.post('DEEPSEEK', '/chat/completions', payload)
        try:
            message = data['choices'][0]['message']
            if not isinstance(message, dict):
                raise ValueError()
            return message
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError('deepseek_invalid_response') from None
