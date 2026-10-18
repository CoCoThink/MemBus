# ChatGPT Web via Secure MCP Tunnel

Status: personal single-user design

## Principle

ChatGPT Web is another MCP client.

```text
Local agent ── stdio ───────────┐
                                ▼
                            membus-mcp
                                │
                                ▼
                              MemBus
                                │
                                ▼
                           SQLite + FTS5
                                ▲
                                │
ChatGPT Web ─ Secure MCP Tunnel ┘
```

Secure MCP Tunnel changes connectivity only. It does not create a second adapter, schema, ranking policy, or permission model.

## Tool surface

Both local and tunneled clients see the same eight tools:

```text
memory_search
memory_list
memory_contexts
memory_get
memory_put
memory_update
memory_delete
memory_restore
```

## Deployment boundary

Tunnel IDs, OpenAI credentials, and connector/session metadata stay outside the MemBus database.

For the personal case, the tunnel client launches the same stdio executable used locally:

```text
tunnel-client
    └── stdio -> membus-mcp
```

No MemBus HTTP server or extra authentication layer is required.

## Trust

The user trusts every client they connect, including ChatGPT Web. Tunneled clients get full read/write access, same as local agents; there is no read-only or append-only mode. Retry-created duplicates are already absorbed by idempotent `memory_put` (content-hash dedup).

## Add complexity only when the use case changes

- multiple human users -> identity/ACL may be needed;
- client-specific visibility -> access policy may be needed;
- persistent service/HA -> private HTTP MCP may be appropriate.

None of these are prerequisites for the current personal deployment.

See [Secure MCP Tunnel deployment](secure-tunnel-deployment.md) for the concrete runbook.
