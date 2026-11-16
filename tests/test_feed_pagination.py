"""Exercise the real list route against an isolated synthetic SQLite database."""
import ast
import asyncio
import base64
import binascii
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TestConnection(sqlite3.Connection):
    def close(self):
        pass  # Route owns close; keep this one in-memory fixture for subsequent pages.


class FeedPaginationTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / 'douyin-mcp/common.py').read_text())
        self.columns = ast.literal_eval(next(n.value for n in tree.body if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == '_VIDEO_COLUMNS' for t in n.targets)))
        self.conn = sqlite3.connect(':memory:', factory=TestConnection)
        definitions = [f'{c} INTEGER' if c in ('watched', 'starred', 'watch_later', 'interest_score') else f'{c} TEXT'
                       for c in self.columns]
        self.conn.execute('CREATE TABLE videos (' + ','.join(definitions) + ')')
        self.conn.execute('CREATE TABLE creators (platform TEXT, name TEXT, unlisted INTEGER)')
        for i in range(123):
            self.conn.execute('INSERT INTO videos (id, platform, title, user, published_at, watched, starred, watch_later, labels) '
                              'VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)',
                              (f'{i:03}', 'youtube' if i % 2 else 'douyin', 'Straße %_ video' if i == 0 else f'Video {i}',
                               'Alpha' if i % 3 else 'Beta', None if i < 3 else '2026-11-16T12:00:00',
                               i % 2, i % 2, 'ai_tech' if i % 2 else 'english'))
        tree = ast.parse((ROOT / 'douyin-mcp/server.py').read_text())
        route = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'api_videos')
        route.decorator_list = []
        scope = dict(Request=object, Response=object, base64=base64, binascii=binascii, json=json,
                     _VIDEO_COLUMNS=self.columns, LABEL_CATEGORIES={'ai_tech': 'AI', 'english': 'English'},
                     _db=lambda: self.conn, _json_gz=lambda request, payload: payload,
                     JSONResponse=lambda payload, status_code=200: SimpleNamespace(payload=payload, status_code=status_code))
        exec(compile(ast.Module(body=[route], type_ignores=[]), '<feed-test>', 'exec'), scope)
        self.route = scope['api_videos']

    def tearDown(self):
        sqlite3.Connection.close(self.conn)

    def page(self, **params):
        return asyncio.run(self.route(SimpleNamespace(query_params=params)))

    def test_batches_ties_null_dates_and_end(self):
        pages = []
        params = {'limit': '50'}
        while True:
            page = self.page(**params)
            pages.append(page['videos'])
            if not page['has_more']:
                self.assertIsNone(page['next_cursor'])
                break
            params['cursor'] = page['next_cursor']
        self.assertEqual([len(p) for p in pages], [50, 50, 23])
        ids = [v['id'] for page in pages for v in page]
        self.assertEqual(len(set(ids)), 123)
        self.assertEqual(ids, [f'{i:03}' for i in reversed(range(123))])
        self.assertEqual(len(self.page()['videos']), 123)  # Legacy clients retain their contract.

    def test_watched_changes_and_new_insert_do_not_shift_cursor(self):
        first = self.page(limit='50', watched='false')
        self.conn.executemany('UPDATE videos SET watched=1 WHERE id=?', [(v['id'],) for v in first['videos']])
        self.conn.execute("INSERT INTO videos (id, published_at, watched) VALUES ('new', '2026-11-17', 0)")
        second = self.page(limit='50', watched='false', cursor=first['next_cursor'])
        self.assertEqual(len(second['videos']), 50)
        self.assertEqual(second['videos'][0]['id'], '072')
        self.assertFalse(set(v['id'] for v in first['videos']) & set(v['id'] for v in second['videos']))

    def test_search_reaches_unloaded_rows_and_is_literal_unicode(self):
        self.assertNotIn('000', [v['id'] for v in self.page(limit='50')['videos']])
        self.assertEqual([v['id'] for v in self.page(limit='50', q='STRASSE')['videos']], ['000'])
        self.assertEqual([v['id'] for v in self.page(limit='50', q='%_')['videos']], ['000'])

    def test_filters_and_hidden_creators(self):
        params = dict(limit='50', platform='youtube', user='Alpha', label='ai_tech', starred='true', watch_later='true')
        rows = self.page(**params)['videos']
        self.assertTrue(rows)
        self.assertTrue(all(v['platform'] == 'youtube' and v['user'] == 'Alpha' and v['starred'] and v['watch_later'] for v in rows))
        self.conn.execute("INSERT INTO creators VALUES ('youtube', 'Alpha', 1)")
        self.assertEqual(self.page(**params)['videos'], [])
        self.assertEqual(len(self.page(**params, show_unlisted='true')['videos']), len(rows))

    def test_invalid_pagination_rejected(self):
        for params in ({'limit': '0'}, {'limit': '201'}, {'limit': 'bad'}, {'limit': '50', 'cursor': 'invalid'}, {'cursor': 'abc'}):
            with self.subTest(params=params):
                self.assertEqual(self.page(**params).status_code, 400)


if __name__ == '__main__':
    unittest.main()
