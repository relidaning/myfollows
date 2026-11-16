import ast
import datetime as dt
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import asyncio
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'douyin-mcp'))
sys.path.insert(0, str(ROOT / 'scripts'))
import runtime
from sync_report import aggregate, render


class RuntimeTests(unittest.TestCase):
    def test_background_sync_returns_and_deduplicates(self):
        jobs = runtime.SyncCoordinator()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def worker():
            entered.set()
            release.wait(2)
            finished.set()
        self.assertTrue(jobs.start(worker))
        self.assertTrue(entered.wait(1))
        self.assertTrue(jobs.snapshot()['running'])
        self.assertFalse(jobs.start(lambda: self.fail('duplicate job')))
        self.assertFalse(jobs.run(lambda: self.fail('scheduled duplicate'), 'scheduled'))
        release.set()
        self.assertTrue(finished.wait(1))
        # A synchronous retry waits for the coordinator to finish releasing its lock.
        for _ in range(100):
            if not jobs.snapshot()['running']:
                break
            __import__('time').sleep(.001)
        self.assertFalse(jobs.snapshot()['running'])
        self.assertTrue(jobs.run(lambda: None, 'scheduled'))

    def test_background_failure_releases_job(self):
        jobs = runtime.SyncCoordinator()
        def fail():
            raise RuntimeError('synthetic failure')
        self.assertTrue(jobs.run(fail, 'manual'))
        self.assertFalse(jobs.snapshot()['running'])
        self.assertIn('unexpectedly', jobs.snapshot()['error'])
        self.assertTrue(jobs.run(lambda: None, 'manual'))
        self.assertEqual(jobs.snapshot()['error'], '')

    def test_sync_start_api_is_nonblocking_and_validates_limit(self):
        tree = ast.parse((ROOT / 'douyin-mcp/server.py').read_text())
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'api_sync_start')
        function.decorator_list = []
        entered, release = threading.Event(), threading.Event()
        jobs = runtime.SyncCoordinator()
        def worker(label, limit):
            self.assertEqual((label, limit), ('Manual', 30))
            entered.set()
            release.wait(2)
        scope = dict(Request=object, Response=object,
                     JSONResponse=lambda data, status_code=200: (data, status_code),
                     runtime=SimpleNamespace(SYNC_LIMIT=30, sync_jobs=jobs), _perform_sync=worker)
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<test>', 'exec'), scope)
        async def scenario():
            invalid = await scope['api_sync_start'](SimpleNamespace(query_params={'limit': '0'}))
            self.assertEqual(invalid[1], 400)
            result = await asyncio.wait_for(scope['api_sync_start'](SimpleNamespace(query_params={})), .2)
            self.assertEqual(result[1], 202)
            self.assertEqual(result[0]['status'], 'started')
            self.assertTrue(entered.wait(1))
            duplicate = await scope['api_sync_start'](SimpleNamespace(query_params={}))
            self.assertEqual(duplicate[0]['status'], 'already_running')
        try:
            asyncio.run(scenario())
        finally:
            release.set()

    def test_hourly_retry_and_daily_slot(self):
        with patch.object(runtime, 'SYNC_INTERVAL', 3600), patch.object(runtime, 'SYNC_HOUR', 6):
            now = dt.datetime(2026, 11, 16, 5, 40)
            self.assertEqual(runtime.next_sync_time(now), now.replace(hour=6, minute=0))
            now = dt.datetime(2026, 11, 16, 7, 40)
            self.assertEqual(runtime.next_sync_time(now), now + dt.timedelta(hours=1))

    def test_configuration_independent_of_cwd_and_validated(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'douyin-mcp'), SYNC_INTERVAL_MINUTES='15')
        result = subprocess.run([sys.executable, '-c', 'import runtime; print(runtime.SYNC_INTERVAL)'],
                                cwd='/tmp', env=env, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), '900')
        env['SYNC_INTERVAL_MINUTES'] = '0'
        result = subprocess.run([sys.executable, '-c', 'import runtime'], cwd='/tmp', env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SYNC_INTERVAL_MINUTES', result.stderr)

    def test_log_append_rollover_and_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = runtime.DailyHandler()
            class Clock(dt.datetime):
                current = dt.datetime(2026, 11, 15, 23, 59, tzinfo=dt.timezone.utc)
                @classmethod
                def now(cls, tz=None):
                    return cls.current
            with patch.object(runtime, 'LOG_DIR', root), patch.object(runtime.dt, 'datetime', Clock):
                for ok in (True, False):
                    record = logging.LogRecord('test', logging.INFO, '', 0, 'sync_operation %s',
                        (json.dumps({'platform': 'youtube', 'ok': ok, 'duration_seconds': 2, 'fetched': 3}),), None)
                    handler.handle(record)
                Clock.current += dt.timedelta(minutes=2)
                handler.handle(record)
            rows, malformed = aggregate(root, 2, dt.date(2026, 11, 16))
            first = rows[1]
            self.assertEqual((first['count'], first['failures'], first['fetched']), (2, 1, 6))
            self.assertEqual(rows[3]['count'], 1)
            self.assertEqual(malformed, 0)
            self.assertIn('50%', render(rows, malformed))
            with (root / '2026-11-16.log').open('a') as out:
                out.write('broken\n')
            self.assertEqual(aggregate(root, 2, dt.date(2026, 11, 16))[1], 1)

    def test_scheduler_continues_after_one_platform_fails(self):
        # Extract the scheduler without importing app modules or opening account data.
        tree = ast.parse((ROOT / 'douyin-mcp/server.py').read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_perform_sync')
        from unittest.mock import MagicMock
        httpx = MagicMock()
        client = httpx.Client.return_value.__enter__.return_value
        response = MagicMock()
        response.json.return_value = {'fetched': 3, 'new': 2}
        client.post.side_effect = [RuntimeError('offline'), response]
        scope = dict(httpx=httpx, runtime=runtime, time=__import__('time'), datetime=__import__('datetime'),
                     json=json, _log=MagicMock(), _BASE_URL='http://synthetic')
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<test>', 'exec'), scope)
        scope['_perform_sync']('Test', 30)
        state = runtime.sync_snapshot()
        self.assertFalse(state['douyin']['ok'])
        self.assertTrue(state['youtube']['ok'])
        self.assertFalse(state['youtube']['running'])
        self.assertEqual(client.post.call_count, 2)


if __name__ == '__main__':
    unittest.main()
