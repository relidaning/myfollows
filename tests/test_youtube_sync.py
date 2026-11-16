"""Synthetic Playwright boundaries; never open saved sessions or a database."""
import ast
import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock

SOURCE = Path(__file__).resolve().parents[1] / 'douyin-mcp/youtube.py'


class BrowserError(Exception):
    pass


class YoutubeSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_list_failure_keeps_collected_videos(self):
        result, common = await self.run_sync(videos=[{'user': 'Recent creator'}], following_error=True)
        self.assertEqual(result['fetched'], 1)
        self.assertIn('stopped_early', result)
        common.upsert_videos.assert_called_once()
        self.assertEqual(common.upsert_creators.call_args.args[0][0]['name'], 'Recent creator')

    async def test_following_independent_of_recent_uploads(self):
        result, common = await self.run_sync(videos=[], empty_feed=True)
        self.assertEqual(result['fetched'], 0)
        self.assertEqual(result['creators'], 1)
        self.assertNotIn('stopped_early', result)
        self.assertEqual(common.upsert_creators.call_args.args[0][0]['name'], 'Quiet creator')

    async def run_sync(self, videos, following_error=False, empty_feed=False):
        tree = ast.parse(SOURCE.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'sync')
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_selector = AsyncMock(side_effect=BrowserError() if empty_feed else None)
        page.close = AsyncMock()
        page.locator.return_value.count = AsyncMock(return_value=1)
        ctx = MagicMock(new_page=AsyncMock(return_value=page))
        common = MagicMock()
        common.upsert_videos.return_value = (len(videos), len(videos))
        following = AsyncMock(return_value=[{'name': 'Quiet creator', 'avatar_url': '', 'unread_count': 0}])
        if following_error:
            following.side_effect = BrowserError('changed selector')
        scope = dict(os=MagicMock(), common=common, _headless_lock=asyncio.Lock(),
                     _get_headless_context=AsyncMock(return_value=ctx),
                     SUBSCRIPTIONS_URL='https://synthetic', VIDEO_CARD_SELECTOR='cards',
                     LOGGED_IN_SELECTOR='avatar', PlaywrightTimeoutError=BrowserError,
                     PlaywrightError=BrowserError, _log=MagicMock(), _scroll_load_more=AsyncMock(),
                     _scrape_subscriptions=AsyncMock(return_value=videos), _scrape_following=following)
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<test>', 'exec'), scope)
        result = await scope['sync'](30)
        page.close.assert_awaited_once()
        return result, common
