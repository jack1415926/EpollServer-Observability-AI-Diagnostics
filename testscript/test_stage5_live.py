#!/usr/bin/env python3
"""Independent private SQL/API acceptance; --smoke validates one actual paid diagnosis.

No load generation. Full mode adds a real TCP Worker pause, historical-report
reuse, restart persistence and Python failure isolation. --hold retains servers
for separate browser QA until stdin newline, then performs the same cleanup.
"""
import argparse
from contextlib import ExitStack
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diagnosis.config import load_environment
from benchmark_worker_snapshot import configuration
from import_experiment import upload
from process_group import start_process_group, stop_process_group
from protocol import require, send_pkg, recv_pkg
from test_http_monitor import TOKEN, free_port, identity, request, wait_for
from test_shutdown import children, connect
from test_stage3_live import TestDatabase


def internal(port, token):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.request('GET', '/internal/health', headers={'Authorization': 'Bearer '+token})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def stop(process):
    if process is not None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--hold', action='store_true')
    parser.add_argument('--report', type=Path, help='reuse a saved diagnosis.json for browser QA; requires --smoke')
    parser.add_argument('--reports', type=Path, help='existing experiment report directory; required for full TCP acceptance')
    args = parser.parse_args()
    if args.report and not args.smoke:
        parser.error('--report requires --smoke; no TCP fault or new model call is performed')
    if args.hold and not sys.stdin.isatty():
        parser.error('--hold requires an interactive terminal (PTY)')
    reports = sorted(args.reports.glob('round-*-experiment.json')) if args.reports else []
    if not args.smoke and len(reports) < 2:
        parser.error('full acceptance requires --reports with at least two existing round-*-experiment.json files')
    if args.smoke and args.reports:
        parser.error('--reports is only used by full acceptance')
    load_environment()
    evidence = Path(tempfile.mkdtemp(prefix='stage5-live-', dir=ROOT/'artifacts'))
    evidence.chmod(0o700)
    print(f'Evidence: {evidence}', flush=True)
    processes = []
    with ExitStack() as cleanup:
        database = TestDatabase(evidence/'database')
        environment = database.__enter__()
        cleanup.callback(database.__exit__, None, None, None)

        def sql(statement):
            return subprocess.run([*database.admin, '--raw', '-Nse', statement], capture_output=True,
                                  text=True, check=True, timeout=10).stdout

        sql("GRANT SELECT,INSERT,UPDATE ON epoll_monitor_test.monitor_event TO 'monitor_test'@'127.0.0.1';"
            "GRANT SELECT,INSERT,UPDATE,DELETE ON epoll_monitor_test.diagnosis_task TO 'monitor_test'@'127.0.0.1';"
            "GRANT SELECT,INSERT,UPDATE,DELETE ON epoll_monitor_test.diagnosis_report TO 'monitor_test'@'127.0.0.1';")
        # One actual task + report transaction/readback before starting the full chain.
        from diagnosis.store import Store
        from unittest.mock import patch
        with patch.dict(os.environ, environment):
            store = Store()
            store.initialize()
            sample_id = '00000000-0000-0000-0000-000000000001'
            store.create(sample_id, 'SQL preflight')
            sample = {'schema_version': 1, 'facts': [], 'unknowns': ['preflight only']}
            store.update(sample_id, 'succeeded', report=sample)
            # Idempotent completion retry must not create another report.
            store.update(sample_id, 'succeeded', report=sample)
            require(store.read(sample_id)['report'] == sample, 'actual report SQL roundtrip failed')
        sql("DELETE FROM epoll_monitor_test.diagnosis_report; DELETE FROM epoll_monitor_test.diagnosis_task")
        print('PASS: private SQL task/report write, readback and completion retry', flush=True)
        http_port, diag_port, tcp_port = free_port(), free_port(), free_port()
        environment.update(EPOLL_MONITOR_TOKEN=TOKEN, EPOLL_MONITOR_PORT=str(http_port), EPOLL_DIAG_PORT=str(diag_port),
                           EPOLL_MONITOR_EVENT_TRIGGER_MS='500', EPOLL_MONITOR_EVENT_RECOVERY_MS='500')
        manifest = evidence/'missing-master.json'
        tcp = None
        if not args.smoke:
            tcp_root = evidence/'tcp'; tcp_root.mkdir(mode=0o700)
            config = tcp_root/'nginx.conf'
            config.write_text(configuration((ROOT/'server/nginx.conf').read_text(), tcp_port, True))
            config.chmod(0o600)
            tcp_log = cleanup.enter_context((evidence/'tcp.log').open('wb'))
            tcp = start_process_group([str(ROOT/'server/nginx')], cwd=tcp_root, env=environment,
                                      stdout=tcp_log, stderr=subprocess.STDOUT)

            def stop_tcp():
                stop_process_group(tcp)
                config.unlink(missing_ok=True)
            cleanup.callback(stop_tcp)

            def tcp_ready():
                require(tcp.poll() is None, 'test TCP exited')
                return len(list((tcp_root/'metrics').glob('worker-*.json'))) == 2
            wait_for(tcp_ready, timeout=30)
            manifest = next((tcp_root/'metrics').glob('master-*.json'))
            with connect(tcp_port, '127.0.10.51') as client:
                send_pkg(client, 0, b'')
                require(recv_pkg(client) == (0, b''), 'actual TCP Ping failed')

        log = cleanup.enter_context((evidence/'services.log').open('ab'))
        monitor = subprocess.Popen([str(ROOT/'monitor/build/epoll-monitor'), str(manifest), str(http_port)],
            cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
        processes.append(monitor); cleanup.callback(stop, monitor)
        wait_for(lambda: monitor.poll() is None and safe(lambda: request(http_port, 'health')[0] == 200))
        diag = None

        def start_diag(env=environment):
            process = subprocess.Popen([sys.executable, '-m', 'diagnosis.service'],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            processes.append(process); cleanup.callback(stop, process)

            def ready():
                require(process.poll() is None, f'diagnosis exited; see {evidence}/services.log')
                return safe(lambda: internal(diag_port, env['EPOLL_DIAG_TOKEN'])[1]['data']['ready'])
            wait_for(ready, timeout=15)
            return process

        diag = start_diag()
        for endpoint, token, method, body, expected in [
            ('diagnoses', None, 'GET', None, 401), ('diagnoses?limit=200', TOKEN, 'GET', None, 400),
            ('diagnoses', TOKEN, 'POST', '{}', 400), ('diagnoses', TOKEN, 'POST', '{"question":" "}', 400),
            ('diagnoses', TOKEN, 'DELETE', None, 405), ('diagnoses/not-a-uuid', TOKEN, 'GET', None, 400),
            ('diagnoses', TOKEN, 'POST', 'x'*4097, 413)]:
            require(request(http_port, endpoint, token, method, body)[0] == expected, 'diagnosis boundary failed')
        paused = None
        if not args.smoke:
            ids = [upload(f'http://127.0.0.1:{http_port}', json.loads(p.read_text()), TOKEN) for p in reports[:2]]
            (evidence/'historical-report-provenance.json').write_text(json.dumps([
                {'id': key, 'source': str(path.resolve())} for key, path in zip(ids, reports)]))
            wait_for(lambda: request(http_port)[1]['validity'] == 'complete')
            pid = children(tcp.pid)[0]
            paused = (pid, identity(pid))

            def resume():
                if paused is not None:
                    require(identity(paused[0]) == paused[1], 'Worker identity changed; no signal sent')
                    os.kill(paused[0], signal.SIGCONT)
            cleanup.callback(resume)
            os.kill(pid, signal.SIGSTOP)
            wait_for(lambda: request(http_port)[1]['validity'] != 'complete', timeout=12)
            wait_for(lambda: any(e['rule'] == 'collection_unavailable' for e in request(http_port, 'events')[1]['data']['events']), timeout=12)

        def submit(question):
            code, value, _ = request(http_port, 'diagnoses', method='POST', body=json.dumps({'question': question}))
            require(code == 202, f'submission failed: {value["code"]}')
            return value['data']['id']

        def finished(key):
            code, value, _ = request(http_port, 'diagnoses/'+key)
            require(code == 200, 'task query failed')
            row = value['data']
            return row if row['status'] not in ('queued', 'running') else None

        start = time.monotonic()
        if args.report:
            original = json.loads(args.report.read_text())
            key = original['id']
            with patch.dict(os.environ, environment):
                store.create(key, original['question'])
                store.update(key, 'succeeded', report=original['report'])
            record = finished(key)
            (evidence/'replayed-report-source.txt').write_text(str(args.report.resolve())+'\n')
        else:
            key = submit('请分析当前 Worker 采集异常，区分当前观测、历史实验和技术文档。说明哪些原因只是推测、哪些信息不足，并建议验证步骤。不要将采集异常等同于业务故障。')
            record = wait_for(lambda: finished(key), timeout=120)
        require(record['status'] == 'succeeded', f'diagnosis failed: {record.get("error_code")}')
        report = record['report']
        refs = {e['id'] for e in report['evidence']}
        require(all(ref in refs for item in report['analysis']['facts']+report['analysis']['possible_causes'] for ref in item['evidence_ids']), 'fabricated citation')
        require(any(e['source'] == 'technical_docs' and e['scope'] != 'unavailable' for e in report['evidence']), 'real hybrid retrieval missing')
        require(report['analysis']['unknowns'], 'missing uncertainty disclosure')
        (evidence/'diagnosis.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
        actual = json.loads(sql(f"SELECT report FROM epoll_monitor_test.diagnosis_report WHERE id='{key}'"))
        require(actual == report, 'persisted actual diagnostic report differs')
        print(f'PASS: {"replayed report" if args.report else "actual remote diagnosis"}, citations and SQL readback ({time.monotonic()-start:.1f}s)', flush=True)
        if paused is not None:
            resume(); paused = None
            wait_for(lambda: request(http_port)[1]['validity'] == 'complete')

        stop(diag); diag = start_diag()
        require(request(http_port, 'diagnoses/'+key)[1]['data']['report'] == report, 'diagnostic report lost on restart')
        if not args.smoke:
            # Seed interrupted work as if the process had died before committing completion.
            sql("INSERT INTO epoll_monitor_test.diagnosis_task (id,question,status,created_at_unix_ms,updated_at_unix_ms,expires_at_unix_ms) "
                "VALUES ('00000000-0000-0000-0000-000000000002','interrupted work','running',1,1,9999999999999)")
            stop(diag); diag = start_diag()
            require(request(http_port, 'diagnoses/00000000-0000-0000-0000-000000000002')[1]['data']['status'] == 'interrupted', 'restart falsely resumed old work')
            stop(diag)
            require(request(http_port, 'diagnoses')[0] == 503, 'dead Python service not isolated')
            require(request(http_port, 'health')[0] == 200 and request(http_port)[1]['validity'] == 'complete', 'AI failure affected monitor')
            with connect(tcp_port, '127.0.10.52') as client:
                send_pkg(client, 0, b'')
                require(recv_pkg(client) == (0, b''), 'AI failure affected TCP')
            broken = {**environment, 'DEEPSEEK_API_KEY': 'invalid-test-key'}
            diag = start_diag(broken)
            failed_key = submit('请分析当前采集状态并明确证据不足。')
            failed = wait_for(lambda: finished(failed_key), timeout=120)
            require(failed['status'] == 'failed' and failed['error_code'] == 'deepseek_http_401', 'AI auth failure falsely succeeded')
            require(request(http_port, 'health')[0] == 200, 'AI provider failure affected monitor')
            stop(diag); diag = start_diag()
        print('PASS: restart persistence and fault isolation', flush=True)
        if args.hold:
            print(f'Browser QA: http://127.0.0.1:{http_port}/#diagnoses ; token is test_http_monitor.TOKEN', flush=True)
            (evidence/'browser.json').write_text(json.dumps({'port': http_port, 'task_id': key}))
            input('Press Enter to clean up: ')
    require(all(p.poll() is not None for p in processes) and not database.root.exists(), 'resource cleanup incomplete')
    if tcp is not None:
        require(tcp.poll() is not None and not config.exists(), 'TCP cleanup incomplete')
    print('PASS: test processes exited; private MySQL SHUTDOWN; test config and data removed', flush=True)


def safe(check):
    try: return check()
    except (OSError, KeyError, TypeError): return False


if __name__ == '__main__':
    main()
