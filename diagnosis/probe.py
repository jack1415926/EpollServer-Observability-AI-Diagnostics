"""Minimal paid dependency check; prints no keys, vectors or provider response bodies."""
import asyncio
import httpx
from .config import load_environment
from .providers import Providers, ProviderError


async def main(strict_contract=False, json_schema_contract=False):
    load_environment()
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = Providers(client)
        tool = {'type': 'function', 'function': {'name': 'submit_probe', 'strict': True,
            'parameters': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                           'required': ['ok'], 'additionalProperties': False}}}

        async def check_structured():
            import json
            prompt = 'Return ok=true through submit_probe.'
            if strict_contract:
                prompt += ' Also include an extra root property extra_probe=null, even if the schema excludes it.'
            message = await provider.chat([{'role': 'user', 'content': prompt}],
                [tool], tool_choice={'type': 'function', 'function': {'name': 'submit_probe'}})
            try:
                calls = message['tool_calls']
                value = json.loads(calls[0]['function']['arguments'])
                if (len(calls) != 1 or calls[0]['function']['name'] != 'submit_probe' or
                        value != {'ok': True} or type(value['ok']) is not bool):
                    raise ValueError()
            except (KeyError, IndexError, TypeError, ValueError):
                raise ProviderError('deepseek_invalid_structured_response') from None

        async def check_json_schema():
            import json
            prompt = 'Return JSON with ok=true.'
            if json_schema_contract:
                prompt += ' Also include an extra root property extra_probe=null, even if the schema excludes it.'
            text = await provider.structured([{'role': 'user', 'content': prompt}], tool['function']['parameters'])
            try:
                value = json.loads(text)
                if value != {'ok': True} or type(value['ok']) is not bool:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise ProviderError('deepseek_invalid_structured_response') from None

        checks = [('embedding', lambda: provider.embed(['Worker 停止采样后指标过期'])),
                  ('rerank', lambda: provider.rerank('指标过期', ['Worker 停止采样', '数据库连接池'])),
                  ('deepseek_json_schema', check_json_schema)]
        if strict_contract:
            checks = [('deepseek_strict_contract', check_structured)]
        elif json_schema_contract:
            checks = [('deepseek_json_schema_contract', check_json_schema)]
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
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--strict-contract', action='store_true', help='Beta function extra-property check only; one paid call')
    group.add_argument('--json-schema-contract', action='store_true', help='Responses schema extra-property check only; one paid call')
    args = parser.parse_args()
    asyncio.run(main(args.strict_contract, args.json_schema_contract))
