# Secure MCP Tunnel deployment

Status: personal single-user deployment

MemBus uses the same `membus-mcp` stdio server for local agents and ChatGPT Web. Secure MCP Tunnel only changes connectivity.

## 1. Install MemBus

Use a dedicated virtual environment so the tunnel profile can reference a stable absolute executable path.

```bash
git clone https://github.com/CoCoThink/MemBus.git
cd MemBus

python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[mcp]'
```

Initialize and verify the intended canonical store:

```bash
membus --db "$HOME/.membus/memory.db" doctor
```

A healthy doctor report exits with status 0 and reports all application/data checks as `ok: true`.

## 2. Verify the local MCP server first

Before adding the tunnel, verify that the same server you will tunnel can start locally:

```bash
MEMBUS_DB="$HOME/.membus/memory.db" \
  "$PWD/.venv/bin/membus-mcp"
```

The process uses stdio for MCP, so it is expected to wait for protocol input rather than print an interactive prompt.

Stop it before starting tunnel-client.

## 3. Create the Secure MCP Tunnel profile

Obtain:

- a tunnel ID from OpenAI Platform tunnel settings
- a runtime API key allowed to use the tunnel
- the current `tunnel-client` binary

Keep the runtime API key outside the repository:

```bash
export CONTROL_PLANE_API_KEY='sk-...'
```

From the MemBus checkout, create a named stdio profile using the absolute MCP executable path:

```bash
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile membus \
  --tunnel-id tunnel_0123456789abcdef0123456789abcdef \
  --mcp-command "$PWD/.venv/bin/membus-mcp"
```

The tunnel profile owns connectivity configuration. MemBus does not store the tunnel ID or OpenAI API key in SQLite.

## 4. Set the MemBus database environment

`tunnel-client` launches `membus-mcp` as a child process, so the child inherits the environment.

```bash
export MEMBUS_DB="$HOME/.membus/memory.db"
```

## 5. Run both health checks

Application/data health:

```bash
membus --db "$MEMBUS_DB" doctor
```

Tunnel/control-plane health:

```bash
tunnel-client doctor --profile membus --explain
```

Do not troubleshoot ChatGPT discovery until both layers are healthy enough to proceed.

## 6. Run the tunnel

```bash
tunnel-client run --profile membus
```

Keep this process running while ChatGPT or another OpenAI surface is using the tunnel.

For stdio bindings, use only one active `tunnel-client` instance for a given tunnel ID. Two active instances would launch two independent MCP child processes and requests are not pinned to one child.

## 7. Connect from ChatGPT

In a ChatGPT workspace that supports custom MCP servers and has the required tunnel permissions:

1. Add a custom MCP app/server.
2. Choose the Secure MCP Tunnel connection path.
3. Select the configured tunnel.
4. Let ChatGPT discover the existing MemBus tools.
5. Verify a read first with `memory_search`.
6. Verify a write with a harmless test memory, then remove it with `memory_delete`.

ChatGPT should see the same five tools as local MCP clients:

```text
memory_put
memory_get
memory_update
memory_delete
memory_search
```

## 8. Minimal end-to-end smoke test

Seed one memory locally:

```bash
membus --db "$MEMBUS_DB" put \
  --title 'MemBus tunnel smoke test' \
  --content 'The Secure MCP Tunnel smoke-test token is membus-tunnel-42.' \
  --memory-type fact
```

Then, from ChatGPT, ask it to use MemBus to search for:

```text
membus-tunnel-42
```

If the memory is returned, the full path is working:

```text
ChatGPT
  -> Secure MCP Tunnel
  -> tunnel-client
  -> stdio
  -> membus-mcp
  -> MemBus
  -> SQLite / FTS5
```

## 9. Troubleshooting order

### MemBus doctor fails

Fix the local database/application problem first.

Useful command:

```bash
membus --db "$MEMBUS_DB" doctor
```

### tunnel-client doctor fails

Fix tunnel ID, runtime API key, Platform permissions, outbound connectivity, or the MCP command path.

```bash
tunnel-client doctor --profile membus --explain
```

### Tunnel is healthy but tools are not visible in ChatGPT

Check ChatGPT workspace permissions/custom MCP configuration and refresh tool discovery before changing MemBus.

### MCP child cannot start

Use an absolute executable path in `--mcp-command` and confirm it works manually:

```bash
"$PWD/.venv/bin/membus-mcp"
```

### Search works locally but not through ChatGPT

Use the same `MEMBUS_DB` environment value for the tunnel process. A common mistake is accidentally starting the child with the default `~/.membus/memory.db` while testing a different local database.

## 10. Optional systemd service

For an always-on Linux host, run `tunnel-client` under a supervisor. The MemBus MCP process remains a child of tunnel-client.

See:

- `deploy/systemd/membus-tunnel.service.example`
- `deploy/systemd/membus-tunnel.env.example`

Run `tunnel-client init --profile membus ...` as the same operating-system user used by the service so that the named profile is available to that process.

## 11. Responsibility split

```text
membus doctor
  -> SQLite / schema / WAL / FTS / integrity / write readiness

tunnel-client doctor
  -> control plane / tunnel / MCP child connectivity

ChatGPT custom MCP settings
  -> workspace permission / app installation / tool discovery
```

Do not merge these three layers into one MemBus abstraction.
