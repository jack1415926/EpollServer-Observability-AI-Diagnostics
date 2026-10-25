"""Minimal paid dependency check; prints no keys, vectors or provider response bodies."""
import asyncio
import httpx
from .config import load_environment
from .providers import Providers, ProviderError


async def main():
    load_environment()
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = Providers(client)
        checks = [('embedding', lambda: provider.embed(['Worker 停止采样后指标过期'])),
                  ('rerank', lambda: provider.rerank('指标过期', ['Worker 停止采样', '数据库连接池'])),
                  ('deepseek', lambda: provider.chat([{'role': 'user', 'content': 'Return JSON: {"ok":true}'}]))]
        success = True
        for name, call in checks:
            try:
                value = await call()
                print(name, 'OK', getattr(value, 'shape', ''), flush=True)
            except ProviderError as exc:
                success = False
                print(name, str(exc), flush=True)
        if not success:
            raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
