# ChatGPT Web via Secure MCP Tunnel

Status: proposed deployment/security architecture  
Goal: expose a private MemBus instance to ChatGPT Web through OpenAI Secure MCP Tunnel without making MemBus publicly reachable.

## 1. Current product constraint

As of the current ChatGPT product documentation, full custom MCP server support on ChatGPT Web is available to Business and Enterprise/Edu workspaces. Pro can connect custom MCP servers with read/fetch permissions; Plus is not currently listed as supporting custom MCP server connections.

This is a product-surface constraint, not a MemBus or Secure MCP Tunnel protocol constraint. MemBus should still be designed so the same private tunnel target can be used by supported ChatGPT workspaces, Codex, or the Responses API.

## 2. Recommended topology

For a personal/single-user deployment, keep the canonical store private and keep stdio as the first tunnel binding:

```text
ChatGPT Web
    │
    │ custom MCP app
    ▼
OpenAI-hosted tunnel endpoint
    │
    │ outbound-only tunnel transport
    ▼
tunnel-client
    │
    │ stdio child process
    ▼
membus-chatgpt-mcp
    │
    ▼
MemoryService
    │
    ▼
SQLite canonical store
```

The tunnel client authenticates to OpenAI. MemBus itself stays on the local machine and has no public listener.

The existing generic `membus-mcp` tool surface should not be used unchanged for remote ChatGPT access. Add a dedicated restricted adapter/profile.

## 3. Why not move directly to HTTP

Secure MCP Tunnel supports both:

- stdio child-process MCP
- private Streamable HTTP MCP

Stdio is the lowest-complexity choice for one private user:

- no local TCP listener
- no local TLS/auth stack
- tunnel-client owns the child lifecycle
- the SQLite store remains local

However, stdio has an important limitation: it has no HTTP-header hop. Connector-forwarded Authorization headers and static MCP headers cannot be delivered to a stdio child.

Therefore:

### Use stdio when

- one user owns the MemBus instance
- one ChatGPT connection/tunnel is trusted as that user
- per-user OAuth identity is not needed
- one active tunnel-client instance per tunnel ID is acceptable

### Move to private Streamable HTTP when

- several ChatGPT users share one MemBus deployment
- MemBus must distinguish caller identity
- OAuth bearer tokens must reach MemBus
- static local credentials or MCP-side mTLS are required
- MemBus should run independently of tunnel-client child lifecycle
- multiple deployment components need to reach the same MCP server

The core MemoryService and SQLite schema should not depend on this transport decision.

## 4. Add a ChatGPT-specific adapter policy

Create a separate adapter module, for example:

```text
src/membus/adapters/
├── mcp_server.py
├── chatgpt_mcp.py
└── policy.py
```

Do not expose every core field just because the core supports it.

The current generic MCP adapter allows callers to provide fields such as:

- `actor`
- `trust`
- `confidence`
- arbitrary update `changes`

That is too permissive for a remote LLM-facing adapter.

### Remote adapter rules

The ChatGPT adapter should:

- set `actor` server-side, e.g. `chatgpt-web`
- never allow the model to impersonate another actor
- assign or clamp `trust` server-side
- clamp allowed `importance`
- restrict writeable scopes
- disallow direct mutation of provenance/security fields
- disable physical deletion
- optionally disable logical deletion by default
- record the remote adapter/profile as provenance

The model may suggest metadata. It should not be authoritative for trust or identity.

## 5. Introduce access visibility distinct from scope

Current MemBus scope answers:

> Which project/workspace does this memory apply to?

It does not answer:

> Which clients are allowed to export this memory outside the local trust boundary?

Secure MCP Tunnel prevents a public inbound endpoint, but MCP requests, tool arguments, and responses still traverse OpenAI product/runtime and tunnel infrastructure before the final local hop.

Therefore MemBus needs a separate visibility/access dimension.

Recommended initial model:

```text
visibility:
  local_only
  trusted_agents
  chatgpt
```

or a more general audience table later.

Example:

```yaml
scope: project
project: payment
visibility: chatgpt
```

A memory with:

```yaml
visibility: local_only
```

must never be returned by the ChatGPT adapter, even when it is textually relevant.

This filter belongs before ranking, not as a weak score.

## 6. Use a restricted remote tool surface

Recommended ChatGPT v1 tool profile:

```text
memory_search
memory_get
memory_context
memory_put_candidate
```

Optional later:

```text
memory_update_content
memory_supersede
```

Do not remotely expose a generic arbitrary patch tool.

### Read tools

#### memory_search

Return:

- ID
- title
- concise snippet
- type
- scope
- score
- provenance summary

Do not return the full content of every Top-K hit by default.

#### memory_get

Fetch one selected full record after search.

This two-step pattern reduces unnecessary data egress.

#### memory_context

A higher-level read tool:

```json
{
  "task": "explain why settlement cannot run concurrently",
  "workspace": "work",
  "project": "payment",
  "max_memories": 6,
  "max_chars": 12000
}
```

The server performs:

```text
lexical retrieval
→ dedup
→ scope/visibility filter
→ rerank
→ context packing
```

This reduces repeated ChatGPT tool round-trips.

### Write tools

#### memory_put_candidate

Remote ChatGPT writes should initially create a candidate or low-trust memory, not silently create a high-trust canonical rule.

Possible policy:

```text
ChatGPT write
  ↓
candidate / low-trust active record
  ↓
explicit user review or later promotion
```

The existing local CLI/MCP can retain full-authority explicit writes.

## 7. Make remote writes idempotent

A network/tunnel/product call may be retried after a timeout or connection failure.

The current `memory_put` always generates a new UUID. If the same tool call is retried, MemBus can create duplicate memories.

Add an idempotency boundary before making remote writes production-worthy.

Recommended request field:

```text
idempotency_key
```

or server-generated tool-call/request identity when available.

Persistence options:

```text
request_dedup
─────────────
client
idempotency_key
operation
result_memory_id
created_at

UNIQUE(client, idempotency_key)
```

The transaction becomes:

```text
BEGIN IMMEDIATE
  lookup request key
  if seen → return original result
  otherwise perform mutation
  record request/result
COMMIT
```

This is more important once MemBus crosses a network boundary.

## 8. Freeze and version the ChatGPT MCP contract

ChatGPT workspace apps can use a reviewed/frozen snapshot of MCP tools. Tool definition changes are not necessarily applied automatically and may require an admin refresh/review.

Therefore the remote tool schema should be much more stable than internal Python APIs.

Recommended rules:

- introduce `MCP_CONTRACT_VERSION = 1`
- prefer adding optional fields over changing required fields
- do not rename stable tools casually
- do not reuse a field with a new semantic meaning
- version incompatible tool surfaces explicitly
- add contract tests that snapshot tool names/input schemas

For example:

```text
memory_search        # stable v1
memory_get           # stable v1
memory_context       # stable v1
memory_put_candidate # stable v1
```

If a future breaking design is required, prefer a new explicit tool rather than silently changing the old one.

## 9. Separate remote identity from model-supplied metadata

For the stdio tunnel profile, the backend cannot receive connector HTTP Authorization headers because stdio has no HTTP-header hop.

Treat the whole tunnel binding as one configured identity:

```text
MEMBUS_CLIENT_ID=chatgpt-personal
MEMBUS_ACTOR=chatgpt-web
MEMBUS_POLICY_PROFILE=chatgpt-personal
```

These values are environment/configuration, not tool arguments.

If multiple users later share the service, switch to private Streamable HTTP plus an authentication layer. Secure MCP Tunnel can forward connector Authorization headers to an HTTP MCP origin.

## 10. Add remote policy profiles

Recommended profiles:

### local-full

For local trusted tools:

```text
read all allowed local namespaces
write active/canonical memory
update
logical delete
administrative metadata
```

### chatgpt-readonly

```text
search/get/context
visibility <= chatgpt
no writes
```

### chatgpt-personal

```text
search/get/context
visibility <= chatgpt
put_candidate
no trust escalation
no actor override
no arbitrary update
no delete by default
```

This policy should live above MemoryService or in an adapter policy layer, not in SQLite-specific code.

## 11. Add output and context budgets

A central personal memory store may contain large records. Do not let an MCP caller accidentally export tens of thousands of characters per search.

Server-enforced limits should include:

```text
max_search_results
max_snippet_chars
max_get_chars
max_context_memories
max_context_chars
```

Recommended starting values:

```text
search results: 10
snippet:        500 chars
context:        6 memories
context total:  12k chars
```

The caller may request a lower budget but must not exceed the server's configured ceiling.

## 12. Keep long maintenance work out of synchronous MCP

Secure MCP Tunnel requests have finite request deadlines and queue limits. MemBus currently has millisecond-scale search, which is ideal.

Do not expose operations such as:

- full import of millions of records
- complete reindex
- embedding-model migration
- database vacuum
- large export

as synchronous ChatGPT tools.

Keep them in:

```text
membus CLI
maintenance daemon
offline job
```

Remote MCP tools should stay bounded and quick.

## 13. Deployment configuration

For a first personal stdio deployment:

```text
tunnel-client
  --tunnel-id <tunnel_id>
  --mcp-command "membus-chatgpt-mcp"
```

Keep the tunnel runtime API key outside the MemBus configuration.

Use environment or file-backed secrets for tunnel-client.

Suggested process boundary:

```text
systemd / launchd / supervisor
├── tunnel-client
│     └── child: membus-chatgpt-mcp
└── ~/.membus/memory.db
```

Do not check tunnel credentials into the MemBus repository.

## 14. Operational readiness

Add a MemBus-side doctor command:

```bash
membus doctor
```

It should verify:

- database opens
- schema version supported
- WAL enabled
- FTS5 available
- integrity check
- canonical/FTS row consistency
- writable transaction check
- backup destination test when requested
- remote policy profile loads

Tunnel readiness remains the responsibility of `tunnel-client doctor`, `/healthz`, `/readyz`, and its local admin UI.

The two health layers should remain separate:

```text
MemBus doctor
  = application/data health

tunnel-client doctor
  = transport/control-plane health
```

## 15. Proposed next schema evolution

Do not make transport fields part of the core memory identity.

A small schema v2 should focus on remote access semantics:

```text
memories
+ visibility

request_dedup
+ client
+ idempotency_key
+ operation
+ result_memory_id
+ created_at
```

Optionally add structured invocation audit fields later:

```text
memory_events
+ client_id
+ request_id
+ transport
```

Do not store OpenAI runtime API keys, OAuth access tokens, or tunnel credentials in SQLite.

## 16. Recommended implementation phases

### Phase A — personal ChatGPT tunnel

Keep stdio.

Implement:

- dedicated ChatGPT adapter
- fixed server-side actor
- remote policy profile
- visibility filter
- bounded outputs
- read tools
- candidate-only writes
- idempotency
- contract snapshot tests

### Phase B — persistent private HTTP service

Only when needed.

Implement:

- Streamable HTTP adapter on loopback/private network
- forwarded Authorization handling
- caller identity mapping
- ACL
- OAuth or trusted gateway auth
- independent MemBus daemon lifecycle

### Phase C — shared workspace memory

Only when several human users share one store.

Implement:

- user/client principals
- per-namespace ACL
- visibility/audience policies
- per-user audit
- least-privilege write permissions

## 17. Architecture target

```text
                         ChatGPT Web
                              │
                       custom MCP app
                              │
                    OpenAI Secure Tunnel
                              │
                      outbound-only path
                              │
                       tunnel-client
                              │
             ┌────────────────┴────────────────┐
             │                                 │
      personal: stdio                  shared: HTTP
             │                                 │
             ▼                                 ▼
   ChatGPT Restricted Adapter        Authenticated MCP Adapter
             │                                 │
             └────────────────┬────────────────┘
                              ▼
                         Policy Layer
                 visibility / ACL / budgets
                              │
                              ▼
                         MemoryService
                              │
                              ▼
                    SQLite Canonical Store
                    + FTS derived index
```

The critical principle remains unchanged:

> Secure MCP Tunnel is a transport boundary. It must not become the owner of MemBus data, identity, trust semantics, or lifecycle.
