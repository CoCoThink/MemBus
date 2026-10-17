# ChatGPT Web via Secure MCP Tunnel

Status: simplified deployment design

## 1. Design goal

MemBus should not have a separate ChatGPT memory system and local memory system.

There is only one MCP server: `membus-mcp`, one `MemoryService`, and one canonical SQLite store.

The difference between local agents and ChatGPT Web is only how they reach the same MCP server.

## 2. Topology

### Local agent

```text
Claude Code / Codex / Cursor
            │
           stdio
            │
            ▼
        membus-mcp
            │
            ▼
       MemoryService
            │
            ▼
        SQLite + FTS5
```

### ChatGPT Web

```text
ChatGPT Web
     │
Secure MCP Tunnel
     │
tunnel-client
     │
    stdio
     │
     ▼
 membus-mcp
     │
     ▼
MemoryService
     │
     ▼
SQLite + FTS5
```

The same executable, same tool definitions, same database, same retrieval logic, and same write semantics are used in both cases.

Secure MCP Tunnel is only a transport bridge.

## 3. Do not create a ChatGPT-specific core

For the first usable version, do not introduce:

- a separate ChatGPT MCP adapter
- separate remote/local tool names
- visibility fields
- remote-only memory types
- candidate-only writes
- ACL
- OAuth identity inside MemBus
- separate ChatGPT ranking
- separate ChatGPT schema

These features should only be added later if a concrete problem requires them.

## 4. Keep the same MCP tool surface

The current six tools remain the canonical V1 MCP contract:

```text
memory_put
memory_get
memory_update
memory_delete
memory_search
memory_search_many
```

ChatGPT Web and local coding agents see the same tools.

Do not add a second set such as `chatgpt_memory_search`, `remote_memory_put`, or `memory_put_candidate` unless real use demonstrates different semantics are necessary.

## 5. Keep transport metadata outside the domain model

Tunnel credentials and connection details are deployment configuration, not memory data.

Do not add `tunnel_id`, OpenAI API keys, ChatGPT sessions, or OAuth tokens to the canonical memory schema.

The tunnel runtime should be configured independently from MemBus.

```text
tunnel-client
     │
     └── launches: membus-mcp
```

The canonical database remains `~/.membus/memory.db` or the path configured with `MEMBUS_DB`.

## 6. Generic improvements that help every MCP client

These improvements are reasonable, but they should be implemented once for all clients rather than as ChatGPT-specific behavior.

### Stable MCP contract

Keep the six tool names and parameter meanings stable. This benefits every MCP client.

### Idempotent writes

If duplicate writes caused by retries become a real problem, add a generic optional `idempotency_key` to normal MemBus write operations.

Do not build a remote-only dedup subsystem.

### memory_context

A future high-level `memory_context` tool may be useful for all agents. It should accept task/project/repo context and return a compact group of relevant memories.

This is a retrieval convenience feature, not a ChatGPT security feature.

### doctor

Add `membus doctor` for application/data checks:

- database readable
- WAL enabled
- FTS5 available
- integrity check
- FTS consistency
- basic write transaction

The tunnel has its own diagnostics.

```text
membus doctor
    = MemBus/data health

tunnel-client doctor
    = tunnel/connectivity health
```

## 7. Deployment model

For the personal single-user case, stdio is enough.

Run the same server used locally through the tunnel client:

```text
tunnel-client
    │
    └── stdio -> membus-mcp
```

No Streamable HTTP server is required.

No local TCP port is required.

No extra MemBus authentication layer is required for the initial personal deployment.

## 8. When should the design become more complicated?

Only introduce more machinery when the use case actually changes.

### Multiple human users share one MemBus

Then consider caller identity, ACL, namespaces, HTTP MCP, and OAuth.

### Some memories must not be available to some clients

Then consider visibility/access policy.

### Automatic agents write untrusted memories

Then consider candidate memories and promotion workflow.

### Duplicate writes occur because of retries

Then add idempotency keys.

These are future problem-driven changes, not prerequisites for ChatGPT Web access.

## 9. Recommended current architecture

```text
                     MemBus Clients
                ┌─────────┴─────────┐
                │                   │
          Local MCP client      ChatGPT Web
                │                   │
              stdio          Secure MCP Tunnel
                │                   │
                └─────────┬─────────┘
                          ▼
                      membus-mcp
                          │
                          ▼
                    MemoryService
                          │
               ┌──────────┴──────────┐
               │                     │
             SQLite                 FTS5
          canonical store       derived index
```

The rule is simple:

> ChatGPT Web is just another MCP client.

Secure MCP Tunnel changes connectivity, not MemBus semantics.


## 10. Deployment runbook

For concrete install, health-check, tunnel-client, smoke-test, and optional systemd steps, see [`secure-tunnel-deployment.md`](secure-tunnel-deployment.md).
