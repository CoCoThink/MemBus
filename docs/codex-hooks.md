# Codex automatic memory recall hook

## Goal

An agent may never choose to call memory_search. That is a trigger failure, not a
retrieval-index failure. For Codex installations with local lifecycle hooks,
MemBus uses one synchronous UserPromptSubmit command hook to supply a small,
read-only set of candidate memories before each user request reaches the agent.

The hook complements MCP. MCP remains the interface for memory_get, explicit
search, and deliberate writes. Both paths use the same SQLite database and
MemBus.search algorithm; there is no separate index, embedding model, daemon,
query history, or automatic memory capture.

## Contract and flow

Codex executes a configured command and sends an event JSON object to stdin.
The hook uses only hook_event_name, prompt, and cwd; session and turn identifiers
are intentionally not stored.

1. For UserPromptSubmit with a nonempty prompt, check the MemBus database.
   If it does not exist, return empty JSON without creating a database.
2. Derive a repository *ranking hint* from the nearest ancestor directory with
   a .git entry. Do not infer workspace, project, or branch. Do not exclude
   other repositories.
3. Call MemBus.search(prompt, repo=hint, strict=False, limit=3). Search is
   lexical, without a model call, and may return candidate memories from
   another context.
4. Emit one JSON object using
   hookSpecificOutput.hookEventName="UserPromptSubmit" and
   hookSpecificOutput.additionalContext. Each candidate includes ID, type,
   title, explicit workspace/project/repo/branch, and content.
5. The adapter caps the whole injected context at 2,000 characters and emits
   at most 3 records. Content over 450 characters is omitted entirely and
   marked content_omitted; the agent can use memory_get(id) for the full text.
6. If no candidate is available or any step fails, emit {} and exit 0. There
   is no blocking decision, stdout logging, or database mutation.

Input and output (schematic):

```json
{"hook_event_name":"UserPromptSubmit","prompt":"继续处理 WASON 提交","cwd":"/repo/service"}
```

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "MemBus candidate memories ...\n{\"id\":\"mem_...\",\"type\":\"procedure\",\"title\":\"WASON submission\",\"scope\":{\"workspace\":\"A200V3-提交\",\"project\":\"WASON\",\"repo\":\"service\",\"branch\":\"main\"},\"content\":\"...\"}\n"
  }
}
```

The exact header includes a caution that agent-written memories are historical
data, not instructions or a user request. The candidate's stored scope MUST be
checked before acting on it. In particular, a note from an unfamiliar branch
must not be silently treated as a rule for this branch. Codex may present
additionalContext as a model-visible developer message; treat it as visible to
the model and potentially the transcript, not as private telemetry.

## Installation

Install the MemBus package in the Python environment used by Codex:

```bash
python -m pip install -e .
command -v membus-codex-hook
```

Set MEMBUS_DB for Codex's environment if the MCP server uses a nondefault DB.
Otherwise both use ~/.membus/memory.db. The hook intentionally does **not**
create an empty store when this file is missing.

Add a single user-level entry to ~/.codex/hooks.json, using the absolute path
reported by command -v:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/membus-codex-hook",
            "timeout": 3,
            "additionalContextLimit": 2500,
            "statusMessage": "Recalling MemBus memories"
          }
        ]
      }
    ]
  }
}
```

If hooks.json already exists, merge this entry into its hooks object rather
than overwriting existing hooks. The path must refer to a real executable,
not literally /absolute/path/to/membus-codex-hook. Use the local Codex
/hooks interface to review and trust the command when prompted. Do not add
this as a repo-local hook: MemBus is a cross-project user store. Configure it
once per Codex installation. This implementation does not modify user files.

Smoke test without Codex (the output should contain a candidate only if
the selected database has a matching active memory):

```bash
printf '%s\n' '{"hook_event_name":"UserPromptSubmit","prompt":"WASON","cwd":"/tmp"}' \
  | membus-codex-hook
```

## Why only one hook?

- **UserPromptSubmit** supplies a user query, so lookup has a relevance signal.
  It does not depend on the model remembering to call a tool.
- **SessionStart** lacks a task-specific query; preloading the store would
  waste context and increase the risk of unrelated project instructions.
- **PreToolUse/PostToolUse** run too often for this goal; **Stop** must not
  write transient task details as durable memories.
- A **PostCompact** recovery hook is deferred until a real compaction-related
  recall failure is measured.
- A broad memory_list fallback is not used for vague prompts. Dumping recent
  unrelated memories is not a trustworthy substitute for a matched candidate.

The synchronous command has a 3-second Codex timeout and no background tasks.
It reads the DB locally, uses no network and starts no MCP server.

## Limitations and validation

This integration prevents the *missing tool invocation* failure only when the
lexical search can match the prompt. An implicit preference (for example
"prefer uv") may not match an unrelated task prompt. The fixed candidate/context
caps may exclude records. This is deliberate in v1: collect actual missed
prompt→memory pairs before extending retrieval. Do not claim guaranteed recall.

Run the automated tests:

```bash
pytest -q tests/test_codex_hook.py
```

Then validate in a compatible Codex CLI installation: write a memory with full
workspace/project/repo/branch context; start a fresh Codex session with
MemBus MCP search temporarily disabled; submit a prompt containing a distinctive
cue from that memory; verify the hook context is visible to the model and the
agent answers correctly without querying a memory tool. Also verify that
an unrelated memory is not treated as an instruction.

See the official Codex hook contract:
https://developers.openai.com/codex/hooks
