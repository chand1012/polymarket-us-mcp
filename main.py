"""Polymarket US retail REST API exposed as an unauthenticated MCP server."""
from __future__ import annotations

import argparse
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from typing import Any

from dotenv import dotenv_values
from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult
from jsonschema import Draft202012Validator
from mcp.types import ToolAnnotations
from polymarket_us import AsyncPolymarketUS
from polymarket_us.errors import APIConnectionError, APITimeoutError, APIStatusError

CATALOG = json.loads(Path(__file__).with_name('api_catalog.json').read_text())
ENV_FILE = Path(__file__).with_name('.env')
_client: AsyncPolymarketUS | None = None

# Resource methods supplied by the SDK. Other documented endpoints, and the
# cancel/modify methods that discard response bodies, use its request primitive.
SDK_METHODS = {
    'get_events': ('events', 'list', ('query',)),
    'get_event_by_slug': ('events', 'retrieve_by_slug', ('slug',)),
    'get_event_by_id': ('events', 'retrieve', ('id',)),
    'get_markets': ('markets', 'list', ('query',)),
    'get_market_by_id': ('markets', 'retrieve', ('id',)),
    'get_market_by_slug': ('markets', 'retrieve_by_slug', ('slug',)),
    'get_market_book': ('markets', 'book', ('slug',)),
    'get_market_bbo': ('markets', 'bbo', ('slug',)),
    'get_market_settlement': ('markets', 'settlement', ('slug',)),
    'get_open_orders': ('orders', 'list', ('query',)),
    'get_order': ('orders', 'retrieve', ('orderId',)),
    'create_order': ('orders', 'create', ('body',)),
    'preview_order': ('orders', 'preview', ('body',)),
    'cancel_all_open_orders': ('orders', 'cancel_all', ('body',)),
    'create_multiple_orders': ('orders', 'create_many', ('body',)),
    'cancel_multiple_orders': ('orders', 'cancel_many', ('body',)),
    'modify_multiple_orders': ('orders', 'modify_many', ('body',)),
    'close_position_order': ('orders', 'close_position', ('body',)),
    'get_user_positions': ('portfolio', 'positions', ('query',)),
    'get_activities': ('portfolio', 'activities', ('query',)),
    'get_account_balances': ('account', 'balances', ()),
    'get_series': ('series', 'list', ('query',)),
    'get_series_by_id': ('series', 'retrieve', ('id',)),
    'get_sports': ('sports', 'list', ()),
    'get_sports_teams_for_provider': ('sports', 'teams', ('query',)),
    'search': ('search', 'query', ('query',)),
    'get_rfq_trades': ('rfqs', 'trades', ('query',)),
}


def config(*, validate_network: bool = True) -> tuple[str, int, str, str]:
    settings = {**dotenv_values(ENV_FILE), **os.environ}
    host = settings.get('HOST') or '127.0.0.1'
    try:
        port = int(settings.get('PORT') or '8000') if validate_network else 8000
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError('PORT must be an integer between 1 and 65535') from None
    key_id, secret = settings.get('POLYMARKET_KEY_ID'), settings.get('POLYMARKET_SECRET_KEY')
    if not key_id or not secret:
        raise ValueError('POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY are required')
    return host, port, key_id, secret


@asynccontextmanager
async def lifespan(server: FastMCP):
    global _client
    _, _, key_id, secret = config(validate_network=False)
    # Disable SDK retries for all operations, including DELETE mutations.
    _client = AsyncPolymarketUS(key_id=key_id, secret_key=secret, max_retries=0)
    try:
        yield
    finally:
        await _client.close()
        _client = None


class RetailTool(Tool):
    method: str
    path: str
    authenticated: bool

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        errors = sorted(Draft202012Validator(self.parameters).iter_errors(arguments), key=lambda e: str(list(e.path)))
        if errors:
            error = errors[0]
            where = '.'.join(map(str, error.absolute_path)) or 'arguments'
            return ToolResult({'error': 'invalid_arguments', 'message': f'{where}: {error.message}'}, is_error=True)
        if _client is None:
            return ToolResult({'error': 'server_not_ready'}, is_error=True)
        path = re.sub(r'\{([^}]+)\}', lambda match: quote(str(arguments[match.group(1)]), safe=''), self.path)
        is_mutation = self.method != 'GET' and self.path != '/v1/order/preview'
        try:
            if self.name in SDK_METHODS:
                resource, method, keys = SDK_METHODS[self.name]
                sdk_method = getattr(getattr(_client, resource), method)
                result = await sdk_method(*(arguments.get(key) for key in keys))
            else:
                # PUT and newer endpoints are not exposed as SDK resource methods.
                result = await _client._request(self.method, path, query=arguments.get('query'),
                                                body=arguments.get('body'), authenticated=self.authenticated)
            return ToolResult(result if result is not None else {})
        except (APITimeoutError, APIConnectionError) as exc:
            payload = {'error': 'outcome_unknown' if is_mutation else 'upstream_connection_error',
                       'message': ('The request may have reached Polymarket. Check account state before retrying.'
                                   if is_mutation else 'Polymarket could not be reached or timed out.')}
            if getattr(exc, 'request_id', None):
                payload['request_id'] = exc.request_id
            return ToolResult(payload, is_error=True)
        except APIStatusError as exc:
            # Upstream error bodies may reflect submitted fields. Do not echo them or credentials.
            payload = {'error': 'polymarket_api_error', 'status_code': exc.status_code,
                       'message': 'Polymarket rejected the request or returned an error.'}
            if getattr(exc, 'request_id', None):
                payload['request_id'] = exc.request_id
            if is_mutation and (exc.status_code == 408 or exc.status_code >= 500):
                payload['error'] = 'outcome_unknown'
                payload['message'] = 'Polymarket returned a server error; check account state before retrying.'
            return ToolResult(payload, is_error=True)


mcp = FastMCP('Polymarket US', lifespan=lifespan,
              instructions='Tools access Polymarket US retail APIs. Mutating tools can affect real funds. Preview orders before submission.')
for spec in CATALOG:
    mutation = spec['method'] != 'GET' and spec['path'] != '/v1/order/preview'
    mcp.add_tool(RetailTool(**spec, annotations=ToolAnnotations(readOnlyHint=not mutation,
                 destructiveHint=mutation, idempotentHint=not mutation, openWorldHint=True)))


def main() -> None:
    parser = argparse.ArgumentParser(description='Polymarket US MCP server')
    parser.add_argument('--transport', choices=('http', 'stdio'), default='http',
                        help='MCP transport (default: http)')
    args = parser.parse_args()
    if args.transport == 'stdio':
        config(validate_network=False)
        mcp.run(transport='stdio', show_banner=False)
    else:
        host, port, _, _ = config()
        mcp.run(transport='http', host=host, port=port, path='/mcp', show_banner=False)


if __name__ == '__main__':
    main()
