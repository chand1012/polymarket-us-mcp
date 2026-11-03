# Polymarket US MCP server

An MCP server for the Polymarket US retail REST API, supporting streamable HTTP and stdio. It exposes 58 tools for market and event discovery, prices and order books, sports, tags, account and portfolio, orders, combos, RFQs, and incentives. Tool input schemas are derived from the published Polymarket OpenAPI schemas; use your MCP client's tool listing to see available fields, required inputs, and pagination options.

**Trading tools operate on your real account.** The MCP endpoint has no authentication: anyone able to connect can read account information and submit or cancel trades. It binds to localhost by default. Only change `HOST` if the endpoint is protected by a trusted network boundary or an authenticated proxy.

## Setup

Install Python 3.14 and [uv](https://docs.astral.sh/uv/). Create a Polymarket US account and API key as described in [Polymarket's authentication guide](https://docs.polymarket.us/api-reference/authentication). Identity verification and account funding may be required for trading.

```bash
uv sync --locked
cp .env.example .env
# Edit .env and add your Polymarket US key ID and secret key.
uv run python main.py
```

Connect an MCP client using **streamable HTTP** at `http://127.0.0.1:8000/mcp`. Configure that URL in the client's remote MCP server settings. `HOST` and `PORT` select the server's bind address and port; both keys are required on startup. The server reads `.env` from the project directory; existing environment variables take precedence over values in the file. Protect the secret as you would any trading credential; do not commit it to source control.

### Stdio transport

For local MCP clients that launch a server process, use:

```bash
uv run --directory /absolute/path/to/polymarket-us-mcp python main.py --transport stdio
```

For example, a client using an `mcpServers` configuration can launch it with:

```json
{
  "mcpServers": {
    "polymarket-us": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/polymarket-us-mcp", "python", "main.py", "--transport", "stdio"]
    }
  }
}
```

Replace the example path with your project directory and make sure `uv` is available to the client. The server reads the same project `.env` and environment variables in stdio mode. `HOST` and `PORT` apply only to HTTP. Stdio runs as a child of the MCP client and does not open a listening port. Both transports expose the same live trading tools.

## Using the tools

Start with `search`, `get_events`, `get_markets`, `get_market_by_slug`, `get_market_book`, or `get_market_bbo`. List tools accept a `query` object containing the exact documented query parameters, including pagination; for example, `get_events` with `{"query": {"limit": 10, "offset": 0}}`. Responses come directly from Polymarket.

Use `get_account_balances`, `get_user_positions`, and `get_open_orders` to inspect the account. For a limit order, the following example buys two YES contracts at $0.55 each, subject to the market's rules. Replace the slug and values with the intended trade. Prices are USD strings, and quantity is in contracts.

Call `preview_order` first:

```json
{
  "body": {
    "request": {
      "marketSlug": "your-market-slug",
      "intent": "ORDER_INTENT_BUY_LONG",
      "type": "ORDER_TYPE_LIMIT",
      "price": {"value": "0.55", "currency": "USD"},
      "quantity": 2,
      "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    }
  }
}
```

After reviewing the preview, `create_order` takes the same order directly under `body`, without the `request` wrapper. A call to `create_order` submits a **live order**. The full [orders documentation](https://docs.polymarket.us/api-reference/sdks/python/orders) describes intents, order types, time in force, and other options. The server also provides order lookup, modification, cancellation, closing positions, and batch operations. A returned batch cancellation or modification ID does not itself confirm the exchange applied the operation.

Trading and other mutations are never automatically retried. If a mutation times out, loses its connection, or receives an ambiguous server error, the result is `outcome_unknown`: inspect orders, positions, or the relevant RFQ before retrying. API errors include a status and request ID when available; raw error bodies are not returned to avoid reflecting sensitive data.

The server uses the official Python SDK for supported operations and signed SDK requests for documented endpoints that do not have resource methods. The catalog is a snapshot of the public Polymarket US retail OpenAPI schemas and the documented retail combos endpoint; update it when the upstream API changes. WebSocket streaming and institutional APIs are outside this server's scope.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```

Tests use a mocked Polymarket transport, including an actual local HTTP MCP connection; they do not place trades. For optional live read-only verification, start the server with your keys and call `get_markets` or `get_account_balances` from an MCP client. Do not use a live trading tool to verify setup unless you intend to submit a real trade.

## Container

The GitHub Actions workflow builds the image for Linux amd64 and arm64 and publishes it as `ghcr.io/chand1012/polymarket-us-mcp`. Pushes to `main` or `master` publish `latest`; version tags beginning with `v` publish a matching image tag, and published builds also receive a commit SHA tag. Pull requests build and test without publishing.

Build and run locally, supplying credentials at runtime:

```bash
docker build -t polymarket-us-mcp .
docker run --rm --env-file .env -e HOST=0.0.0.0 -e PORT=8000 \
  -p 127.0.0.1:8000:8000 polymarket-us-mcp
```

Replace the image name with `ghcr.io/chand1012/polymarket-us-mcp:latest` to run a published image. The server listens on all interfaces inside the container so Docker can forward requests; the example publishes the port only on the host's loopback interface. To restrict access to a tailnet, replace `127.0.0.1` in the `-p` mapping with the host's Tailscale IPv4 address and apply your Tailscale access rules. Do not set `HOST` inside the container to the host's Tailscale IP, which normally does not exist in the container network namespace. Anyone who can connect to the published port can use the account's trading tools.

To run the stdio transport instead, pass `--transport stdio` after the image name and keep standard input open with `-i`; no port mapping is needed. Do not allocate a TTY for MCP communication.
