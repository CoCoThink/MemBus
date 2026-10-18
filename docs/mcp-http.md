# MCP HTTP deployment

`membus-mcp` supports stdio and Streamable HTTP using the same tools and SQLite
store. Stdio remains the default: the MCP client starts its own MemBus process.
HTTP lets multiple clients connect to one running MemBus service, including a
WSL client and a Windows tunnel client.

## Start in the daily environment

Use the existing `daily` environment where MemBus is installed:

```bash
mamba activate daily
membus-mcp --transport streamable-http \
  --host 127.0.0.1 --port 8301 --http-path /mcp \
  --db "$HOME/.membus/memory.db"
```

The MCP endpoint is `http://127.0.0.1:8301/mcp`. Clients connect using Streamable
HTTP. For a tunnel, configure its MCP server URL to this same endpoint as seen
from the tunnel host.

## Startup configuration

Command-line options take precedence over environment variables.

| Option | Environment variable | Default |
| --- | --- | --- |
| `--transport` | `MEMBUS_TRANSPORT` | `stdio`; also accepts `streamable-http` |
| `--db` | `MEMBUS_DB` | `~/.membus/memory.db` |
| `--host` | `MEMBUS_HOST` | `127.0.0.1` |
| `--port` | `MEMBUS_PORT` | `8301` |
| `--http-path` | `MEMBUS_HTTP_PATH` | `/mcp` |

Host, port, and HTTP path apply to HTTP transport. The port must be between 1
and 65535; the path must start with `/` and contain no query, fragment, or route
parameters. `membus-mcp --help` lists the options without opening a database.

For a service configured through environment variables:

```bash
export MEMBUS_TRANSPORT=streamable-http
export MEMBUS_HOST=127.0.0.1
export MEMBUS_PORT=8301
export MEMBUS_HTTP_PATH=/mcp
export MEMBUS_DB="$HOME/.membus/memory.db"
membus-mcp
```

To explicitly use stdio even when the environment selects HTTP:

```bash
membus-mcp --transport stdio
```

## Windows service and WSL

For a native Windows service, point the service wrapper at the absolute
`python.exe` in the environment where MemBus is installed, with arguments:

```text
-m membus.adapters.mcp_server --transport streamable-http --host 127.0.0.1 --port 8301 --db D:\Data\MemBus\memory.db
```

For `daily` installed inside WSL, the Windows service must use `wsl.exe` to
launch that Linux environment's Python instead. Run it under the Windows
account that owns the intended WSL distribution and specify its Linux user;
a native Windows process cannot directly execute a Linux Python path.

The WSL MCP client connects to the HTTP endpoint; it does not start another
stdio process. With mirrored networking, Windows and WSL can use localhost.
With WSL NAT networking, use the Windows host address and bind the Windows
server to an interface reachable from WSL; firewall rules must allow that
connection. Set `--host` to the appropriate address when needed. See
[Microsoft's WSL networking documentation](https://learn.microsoft.com/windows/wsl/networking).

HTTP has the same trust model as stdio: every connected client has full
read/write access, with no authentication or per-client ACL. The default
binding is loopback; use a trusted network when binding to another interface.

## Verify

Check the same database used by the service:

```bash
membus --db "$HOME/.membus/memory.db" doctor
```

Then use an MCP client to initialize the HTTP connection, list tools, and call
`memory_contexts` or `memory_search`. A browser GET to `/mcp` is not an MCP
handshake and may return a protocol error.
