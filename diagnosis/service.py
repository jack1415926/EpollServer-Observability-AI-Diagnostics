"""Loopback-only internal task service; Drogon remains the public control plane."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import logging
import os
from pathlib import Path
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent import Tools, diagnose
from .config import load_environment, redact
from .providers import Providers, ProviderError
from .retrieval import INDEX, Retriever
from .store import Store


class Submission(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=1000)


def response(status, code, data=None):
    return JSONResponse({'code': code, 'data': data}, status_code=status,
                        headers={'Cache-Control': 'no-store'})


def create_app(store=None, runner=None):
    load_environment()
    store = store or Store()
    token = os.getenv('EPOLL_DIAG_TOKEN', '')
    if not re.fullmatch(r'[A-Za-z0-9\-._~+/=]{32,256}', token):
        raise ValueError('invalid_EPOLL_DIAG_TOKEN')
    timeout = int(os.getenv('EPOLL_DIAG_TIMEOUT_SECONDS', '90'))
    if not 1 <= timeout <= 300:
        raise ValueError('invalid_diagnosis_timeout')
    queue = asyncio.Queue(maxsize=8)
    lock = asyncio.Lock()
    state = {'ready': False, 'outstanding': 0, 'running': 0, 'pending_results': 0}

    async def initialize():
        while not state['ready']:
            try:
                await asyncio.to_thread(store.initialize)
                state['ready'] = True
            except Exception:
                await asyncio.sleep(2)

    async def worker(client, retriever):
        while True:
            key, question, enqueued = await queue.get()
            state['running'] += 1
            status, error, report = 'failed', None, None
            try:
                await asyncio.to_thread(store.update, key, 'running')
                if time.monotonic() - enqueued > 30:
                    status, error = 'timed_out', 'queue_timeout'
                else:
                    async with asyncio.timeout(timeout):
                        report = await (runner(question) if runner else diagnose(question,
                            Providers(client), Tools(client, retriever)))
                    # Redact the model output as well as the evidence sent to it.
                    report = json.loads(redact(report))
                    if len(json.dumps(report).encode()) > 1024*1024:
                        raise ValueError('report_too_large')
                    status = 'succeeded'
            except TimeoutError:
                status, error = 'timed_out', 'diagnosis_timeout'
            except ProviderError as exc:
                error = str(exc)
            except asyncio.CancelledError:
                try:
                    await asyncio.to_thread(store.update, key, 'interrupted', 'service_stopped')
                finally:
                    state['running'] -= 1
                    state['outstanding'] -= 1
                    queue.task_done()
                raise
            except ValidationError as exc:
                logging.getLogger(__name__).warning('diagnosis_invalid_report fields=%s',
                    [(str(item['loc']), item['type']) for item in exc.errors(include_input=False)])
                error = 'diagnosis_invalid_report'
            except Exception as exc:
                import traceback
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                logging.getLogger(__name__).warning('diagnosis_execution_failed type=%s location=%s:%s',
                    type(exc).__name__, frame.name, frame.lineno)
                known = {'fabricated_evidence_reference', 'invalid_tool_calls', 'report_too_large'}
                error = str(exc) if str(exc) in known else 'diagnosis_execution_failed'
            state['pending_results'] += 1
            try:
                # Retain a slot until the terminal result is durable; do not silently lose it on SQL failure.
                while True:
                    try:
                        await asyncio.to_thread(store.update, key, status, error, report)
                        break
                    except Exception:
                        await asyncio.sleep(2)
            finally:
                state['pending_results'] -= 1
                state['running'] -= 1
                state['outstanding'] -= 1
                queue.task_done()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4)) as client:
            retriever = Retriever(Providers(client), Path(os.getenv('EPOLL_DIAG_INDEX_PATH', str(INDEX)))) if runner is None else None
            tasks = [asyncio.create_task(initialize())]
            tasks += [asyncio.create_task(worker(client, retriever)) for _ in range(2)]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def boundary(request, call_next):
        if not hmac.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + token).encode()):
            return response(401, 'unauthorized')
        body, size = [], 0
        async for part in request.stream():
            size += len(part)
            if size > 4096:
                return response(413, 'body_too_large')
            body.append(part)
        request._body = b''.join(body)
        if request.query_params or (request.method == 'GET' and size):
            return response(400, 'unexpected_input')
        try:
            return await call_next(request)
        except Exception:
            return response(503, 'diagnosis_store_unavailable')

    @app.get('/internal/health')
    async def health():
        return response(200, 'ok', {**state, 'queued': queue.qsize(), 'capacity': 10})

    @app.post('/internal/diagnoses')
    async def submit(request: Request):
        try:
            value = Submission.model_validate_json(await request.body())
            question = value.question.strip()
            if not question:
                raise ValueError()
        except (ValidationError, ValueError):
            return response(400, 'invalid_diagnosis_request')
        if not state['ready']:
            return response(503, 'diagnosis_store_not_ready')
        async with lock:
            if queue.full() or state['outstanding'] >= 10:
                return response(503, 'diagnosis_busy')
            key = str(uuid.uuid4())
            try:
                await asyncio.to_thread(store.create, key, redact(question))
            except Exception:
                return response(503, 'diagnosis_store_unavailable')
            state['outstanding'] += 1
            queue.put_nowait((key, redact(question), time.monotonic()))
        return response(202, 'accepted', {'id': key, 'status': 'queued'})

    @app.get('/internal/diagnoses')
    async def listing():
        if not state['ready']:
            return response(503, 'diagnosis_store_not_ready')
        return response(200, 'ok', {'tasks': await asyncio.to_thread(store.read), 'limit': 100})

    @app.get('/internal/diagnoses/{key}')
    async def detail(key: str):
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', key):
            return response(400, 'invalid_diagnosis_id')
        if not state['ready']:
            return response(503, 'diagnosis_store_not_ready')
        value = await asyncio.to_thread(store.read, key)
        return response(200, 'ok', value) if value else response(404, 'diagnosis_not_found')

    return app


if __name__ == '__main__':
    import uvicorn
    load_environment()
    uvicorn.run(create_app(), host='127.0.0.1', port=int(os.getenv('EPOLL_DIAG_PORT', '8090')),
                access_log=False, limit_concurrency=24, timeout_keep_alive=5)
