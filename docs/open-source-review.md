# Open-source Memory System Review

This document records the design lessons MemBus takes from existing projects. It is not a dependency decision and should not be read as an endorsement of copying any project wholesale.

Reviewed directions:

- Model Context Protocol reference `server-memory`
- `RMANOV/sqlite-memory-mcp`
- Mem0
- related temporal/graph systems such as Graphiti and Cognee

## 1. Evaluation criteria

MemBus evaluates other projects against these questions:

1. Is canonical memory user-owned?
2. Is the storage model inspectable?
3. Can the system work without embeddings?
4. Is lexical retrieval a first-class candidate source?
5. Are project/repo scopes explicit?
6. Are mutation history and provenance modeled?
7. Can multiple agent clients share the same memory?
8. Can indexes be rebuilt from canonical data?
9. Does the design avoid coupling memory ownership to one adapter/protocol?
10. How much unrelated agent-platform functionality is bundled into the memory core?

## 2. MCP reference server-memory

Repository:

- https://github.com/modelcontextprotocol/servers/tree/main/src/memory

### Strengths

The reference server has a very small conceptual model:

```text
Entity
├── name
├── entityType
└── observations[]

Relation
├── from
├── to
└── relationType
```

Its tools are correspondingly understandable:

- create entities
- create relations
- add observations
- delete entities/relations/observations
- read graph
- search nodes
- open nodes

This is useful as a compatibility/API reference because agent models can understand it easily.

### Weaknesses for MemBus

The reference implementation is intentionally basic:

- JSONL persistence
- simple search semantics
- no BM25/FTS ranking
- no project/repo scope model
- no memory lifecycle
- no strong provenance model
- process-local mutation coordination rather than a general multi-process database concurrency model

### MemBus decision

**Borrow:**

- the minimal mental model of a memory tool surface
- compatibility semantics where useful

**Do not borrow:**

- JSONL as the canonical database
- full-file retrieval architecture
- substring-only search
- the Entity/Observation model as the only internal representation

MemBus should expose compatibility adapters without allowing those adapters to dictate its canonical schema.

## 3. sqlite-memory-mcp

Repository:

- https://github.com/RMANOV/sqlite-memory-mcp

### Strengths

This project is the closest architectural reference for MemBus because it demonstrates:

- SQLite as the local durable store
- WAL mode
- explicit transaction management
- retry around lock contention
- `BEGIN IMMEDIATE` for read-modify-write flows
- SQLite backup API rather than naive file copying
- FTS5/BM25
- optional vector extensions instead of mandatory vector storage
- provenance
- mutation/event concepts
- project/context-aware ranking
- later-stage temporal/canonical facts
- RRF and multi-signal reranking
- multi-agent use through MCP

Its retrieval direction validates an important MemBus thesis:

> semantic similarity is only one signal; lexical relevance, project affinity, recency, graph proximity, authority, and active task context can all matter.

### Weaknesses for MemBus

The project has evolved into a much broader agent operating environment, with many tools and subsystems beyond the desired MemBus core.

Examples of complexity MemBus does not want in V1:

- task/kanban systems
- collaboration/debate flows
- GUI concerns
- premium/entitlement concerns
- many context/audit/task tables
- a very large MCP tool surface

### MemBus decision

**Directly emulate or reimplement the engineering pattern:**

- SQLite connection setup
- WAL/busy handling
- explicit transaction boundaries
- `BEGIN IMMEDIATE` when appropriate
- SQLite consistent backup
- FTS5 synchronization
- provenance/event ledgers

**Borrow the idea, not the full implementation:**

- contextual reranking
- graph expansion
- temporal facts
- conflict governance

**Do not import:**

- task/collaboration/debate/product-layer subsystems
- a large MCP surface
- unrelated orchestration features

## 4. Mem0

Repository:

- https://github.com/mem0ai/mem0

### Strengths

Mem0 is valuable primarily for the memory-intelligence layer:

- extracting durable facts from conversation
- retrieving existing memories before writing new ones
- user/agent/run identity separation
- entity-aware retrieval
- semantic + lexical retrieval experiments
- memory evaluation/benchmarking
- mature SDK/product experience

A particularly useful design direction is that extraction should consider already-stored memory before adding more. This helps reduce duplicate durable facts.

Another useful direction is conservative mutation: automatic extraction should prefer proposing/adding memory rather than silently rewriting or deleting canonical user memory.

### Architectural mismatch

Mem0's open-source memory path remains strongly centered around embeddings/vector-store retrieval.

For MemBus, the preferred ownership model is:

```text
SQLite canonical memory
      ├── FTS derived index
      ├── vector derived index (optional)
      └── graph projection (optional)
```

rather than:

```text
vector store payload
≈ canonical memory store
```

For coding use cases, lexical candidates must independently enter the candidate pool. A semantic threshold must not be able to suppress an exact issue ID, file path, symbol, commit hash, or configuration key.

### MCP considerations

MemBus also does not want its core memory ownership tied to a hosted MCP endpoint.

### MemBus decision

**Borrow:**

- existing-memory-aware extraction
- candidate memory generation
- identity/scope hygiene
- retrieval evaluation
- entity-aware ranking ideas

**Do not borrow as the canonical architecture:**

- vector-store-as-memory-store
- mandatory embedding path
- hosted MCP as the ownership boundary

## 5. Graphiti

Repository:

- https://github.com/getzep/graphiti

### Strengths

Graphiti is particularly relevant for future temporal memory:

- relationship changes over time
- valid-time reasoning
- provenance-aware temporal knowledge graphs
- multi-hop relationships

This is valuable once a memory store has long-lived facts that change:

```text
2025: service uses Node 18
2026-03: service uses Node 20
2026-09: service uses Node 22
```

### Why not V1

Temporal graph infrastructure adds substantial complexity:

- graph storage/backend
- extraction/model dependencies
- more complex conflict semantics
- traversal/ranking behavior

MemBus should first prove that stale/conflicting facts are a material production problem.

### MemBus decision

Use Graphiti as a future design reference for V7/V8, not as a V1 dependency.

## 6. Cognee

Repository:

- https://github.com/topoteretes/cognee

### Strengths

Cognee demonstrates a broader knowledge-processing architecture combining:

- documents
- conversations
- extracted entities
- graph relationships
- vector representations
- agent-facing memory

It is useful when the problem changes from "retrieve a prior memory" to "reason across a knowledge space."

### Why not V1

For a personal coding-agent memory bus, this introduces too much pipeline and representation complexity before basic lexical/scoped retrieval has been proven insufficient.

### MemBus decision

Study it when entity relationships and cross-document reasoning become recurring evaluation failures.

## 7. Combined architectural conclusion

No reviewed project should be used unchanged as the MemBus core.

The useful decomposition is:

```text
MCP server-memory
    → minimal external API semantics

sqlite-memory-mcp
    → SQLite/FTS/concurrency/provenance engineering

Mem0
    → future extraction + evaluation intelligence

Graphiti/Cognee
    → future temporal/graph design references
```

MemBus itself owns:

- canonical schema
- scope hierarchy
- retrieval contract
- mutation semantics
- lifecycle semantics
- adapter-independent service API
- complexity/evaluation gates

## 8. What MemBus intentionally does differently

### 8.1 Memory is the first-class object

MemBus starts from:

```text
memory
├── content
├── type
├── scope
├── provenance
├── lifecycle
└── related entities
```

rather than requiring all memory to be expressed first as graph observations.

### 8.2 Lexical retrieval can independently generate candidates

The target architecture is:

```text
exact candidates ───┐
FTS candidates ─────┼──> union/fusion ──> rank
vector candidates ──┘        (future)
```

not:

```text
semantic candidates
      ↓
lexical score only boosts those candidates
```

This is important for code identifiers.

### 8.3 Vector is an index, not ownership

A future vector index should be removable and rebuildable:

```bash
rm data/indexes/memory.hnsw
membus rebuild-index vector
```

Canonical memory remains intact.

### 8.4 Automatic intelligence is above the core

Future modules:

```text
extract
deduplicate
resolve conflict
consolidate
reflect
```

must call the same stable memory service used by humans and simple clients.

## 9. Re-evaluation policy

This review should be updated when:

- a referenced project materially changes its storage model
- a project provides a clearly superior compatibility layer
- MemBus reaches a roadmap gate (vector, temporal, graph, ACL, extraction)
- benchmark evidence suggests an adopted assumption is wrong

The purpose of the review is to prevent both NIH syndrome and dependency-by-fashion.
