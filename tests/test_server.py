import asyncio
import base64
import socket
import tempfile
from pathlib import Path
import json
import os
import unittest
from unittest.mock import patch

import httpx
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport
from polymarket_us import AsyncPolymarketUS

import main

KEY = base64.b64encode(bytes(range(32))).decode()
ENV = {'POLYMARKET_KEY_ID': 'test-key', 'POLYMARKET_SECRET_KEY': KEY}


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.env_file = patch.object(main, 'ENV_FILE', Path('/nonexistent/polymarket-test.env'))
        self.env_file.start()
        self.requests = []
        self.status = 200
        self.timeout = False
        async def handler(request):
            self.requests.append(request)
            if self.timeout:
                raise httpx.ReadTimeout('timeout', request=request)
            return httpx.Response(self.status, json={'ok': True}, request=request)
        self.sdk = AsyncPolymarketUS(key_id='test-key', secret_key=KEY, max_retries=0)
        await self.sdk._http.aclose()
        self.sdk._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.factory = patch.object(main, 'AsyncPolymarketUS', return_value=self.sdk)
        self.factory.start()
        self.client = Client(main.mcp)
        await self.client.__aenter__()

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)
        self.factory.stop()
        self.env_file.stop()
        self.env.stop()

    async def test_catalog_and_schema(self):
        tools = await self.client.list_tools()
        self.assertEqual(len(tools), 58)
        order = next(t for t in tools if t.name == 'create_order')
        self.assertIn('body', order.input_schema['properties'])
        self.assertTrue(order.annotations.destructive_hint)
        self.assertEqual(main.config()[:2], ('127.0.0.1', 8000))

    async def test_public_pagination_and_authenticated_order(self):
        await self.client.call_tool('get_events', {'query': {'limit': 5, 'offset': 10}})
        self.assertEqual(self.requests[-1].url.host, 'gateway.polymarket.us')
        self.assertEqual(self.requests[-1].url.params['offset'], '10')
        self.assertNotIn('x-pm-signature', self.requests[-1].headers)
        body = {'marketSlug': 'test-market', 'intent': 'ORDER_INTENT_BUY_LONG',
                'type': 'ORDER_TYPE_LIMIT', 'price': {'value': '0.55', 'currency': 'USD'},
                'quantity': 2, 'tif': 'TIME_IN_FORCE_GOOD_TILL_CANCEL'}
        await self.client.call_tool('preview_order', {'body': {'request': body}})
        self.assertEqual(json.loads(self.requests[-1].content), {'request': body})
        await self.client.call_tool('create_order', {'body': body})
        self.assertEqual(json.loads(self.requests[-1].content), body)
        self.assertEqual(self.requests[-1].url.host, 'api.polymarket.us')
        self.assertEqual(self.requests[-1].headers['x-pm-access-key'], 'test-key')
        self.assertIn('x-pm-signature', self.requests[-1].headers)

    async def test_timeout_never_retries_live_order(self):
        self.timeout = True
        result = await self.client.call_tool('create_order', {'body': {'marketSlug': 'test'}}, raise_on_error=False)
        self.assertTrue(result.is_error)
        self.assertIn('outcome_unknown', result.content[0].text)
        self.assertEqual(len(self.requests), 1)

    async def test_batch_and_put_paths(self):
        tool = next(t for t in await self.client.list_tools() if t.name == 'cancel_multiple_orders')
        self.assertIn('body', tool.input_schema['properties'])
        await self.client.call_tool('cancel_multiple_orders', {'body': {'orders': [{'orderId': 'id', 'marketSlug': 'market'}]}})
        self.assertEqual(self.requests[-1].url.path, '/v1/orders/batched/cancel')
        await self.client.call_tool('accept_quote', {'rfqId': 'rfq', 'quoteId': 'quote', 'body': {'acceptedSide': 'SIDE_BUY'}})
        self.assertEqual(self.requests[-1].method, 'PUT')
        self.assertEqual(self.requests[-1].url.path, '/v1/rfqs/rfq/quotes/quote/accept')

    async def test_http_transport_with_mocked_upstream(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(main.mcp.http_app(path='/mcp'), log_level='error', lifespan='on'))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(200):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            self.assertTrue(server.started)
            async with Client(f'http://127.0.0.1:{port}/mcp') as remote:
                self.assertEqual(len(await remote.list_tools()), 58)
                result = await remote.call_tool('get_account_balances')
                self.assertFalse(result.is_error)
                self.assertEqual(self.requests[-1].url.path, '/v1/account/balances')
        finally:
            server.should_exit = True
            await task
            sock.close()

    async def test_invalid_arguments_do_not_reach_upstream(self):
        result = await self.client.call_tool('create_order', {'body': {}}, raise_on_error=False)
        self.assertTrue(result.is_error)
        self.assertEqual(len(self.requests), 0)

    async def test_server_errors_do_not_leak_secrets(self):
        self.status = 500
        result = await self.client.call_tool('create_order', {'body': {'marketSlug': 'test'}}, raise_on_error=False)
        self.assertTrue(result.is_error)
        self.assertIn('outcome_unknown', result.content[0].text)
        self.assertNotIn(KEY, result.content[0].text)


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_subprocess(self):
        transport = PythonStdioTransport(
            script_path=Path(main.__file__), args=['--transport', 'stdio'],
            env={**ENV, 'PORT': 'invalid-for-http'},
        )
        async with Client(transport) as client:
            tools = await client.list_tools()
            self.assertEqual(len(tools), 58)
            self.assertIn('create_order', {tool.name for tool in tools})
            result = await client.call_tool('create_order', {'body': {}}, raise_on_error=False)
            self.assertTrue(result.is_error)


class ConfigTests(unittest.TestCase):
    def test_missing_credentials_and_invalid_port(self):
        with patch.object(main, 'ENV_FILE', Path('/nonexistent/polymarket-test.env')), patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'POLYMARKET_KEY_ID'):
            main.config()
        with patch.object(main, 'ENV_FILE', Path('/nonexistent/polymarket-test.env')), patch.dict(os.environ, {**ENV, 'PORT': '0'}, clear=True), self.assertRaisesRegex(ValueError, 'PORT'):
            main.config()

    def test_dotenv_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('HOST=127.0.0.2\nPORT=9000\nPOLYMARKET_KEY_ID=file-key\nPOLYMARKET_SECRET_KEY=file-secret\n')
            with patch.object(main, 'ENV_FILE', path), patch.dict(os.environ, {'PORT': '9001'}, clear=True):
                self.assertEqual(main.config(), ('127.0.0.2', 9001, 'file-key', 'file-secret'))

if __name__ == '__main__':
    unittest.main()
