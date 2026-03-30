# Design Doc: Intent — Tool Server

## Purpose

Spec for intent — a **standalone tool server** that any agent harness can consume. Intent holds API keys, OAuth tokens, and session state. It wraps each external capability as a narrow, named intent and exposes it over HTTP as a callable tool. That's the entire abstraction.

Intent is harness-agnostic. It doesn't know about Claude, OpenAI, LangChain, or any specific agent framework. It serves tools over HTTP. Any harness that can make HTTP requests can use it.

This document is designed to be read in full by both humans and agents. A 200k-context model should be able to hold the entire design (this doc + the ~280 LOC implementation) in working memory. If it can't, the design is too complex.

---

## Why Not MCP

MCP (Model Context Protocol) solves discovery and transport across trust boundaries: different tool authors, different hosts, dynamic capability negotiation, schema evolution, server lifecycle management.

Intent is simpler by design. It's a tool server, not an interop protocol:

| MCP feature                                   | Intent                                                                               |
| --------------------------------------------- | ------------------------------------------------------------------------------------ |
| Capability negotiation                        | Static tool registry, known at startup.                                              |
| Transport abstraction (stdio, SSE, HTTP)      | HTTP over Unix domain socket. One transport.                                         |
| Server lifecycle (initialize, ping, shutdown)  | Start with `python -m intent`, stop with Ctrl-C.                                    |
| Schema evolution / versioning                 | Change the schema, restart. No migration.                                            |
| Resource/prompt primitives                    | Tools are the only primitive.                                                        |
| JSON-RPC 2.0 compliance                       | Two REST endpoints. No batch, no notifications.                                      |

**Two HTTP endpoints.** `GET /tools` and `POST /tools/{name}/call`. Any language, any framework, any agent harness can call them. Debug with curl. No custom client library needed.

MCP would work here — it's not wrong. But it's ~5x the protocol surface for zero benefit when intent is a standalone tool server. The complexity cost compounds: every consuming harness needs an MCP client, the server needs the full MCP spec, debugging requires MCP-aware tooling. HTTP is the universal interface.

---

## Core Concept: Tools as Intents

Intent bridges role-based access control to intent-based access control. The bridge is **tool decomposition**.

| | RBAC (traditional) | Intent-based (this design) |
|---|---|---|
| Identity | Role: `email-manager` | Caller with bearer token |
| Permission unit | Capability: `email.read` | Tool: `read_inbox` |
| Granularity | Service-level | Intent-level |
| Implementation | Shared endpoint, parameterized | Separate handler per intent |
| Escalation risk | High (broad capability) | Low (no capability to escalate to) |

**Why service-level permissions fail for agents:**

An RBAC system grants "email.read" — the agent can read any email, search arbitrarily, access attachments, export entire mailboxes. The permission describes the service boundary, not the action boundary. For a human user, that's fine: the user has judgment. For an agent processing untrusted content, every unused capability is attack surface.

**How tool decomposition solves this:**

Each tool is a **specific intent with a specific implementation**:

- `read_inbox` → IMAP fetch, return subjects + senders + snippets. Cannot search, cannot access attachments, cannot export.
- `read_newsletters` → IMAP fetch filtered to newsletter senders, return summaries. Cannot access non-newsletter mail.
- `draft_response` → Create a draft reply. Cannot send.
- `send_email_low_risk` → Send to a list of pre-approved recipients.
- `send_email_to_manager` → You get the picture.

The tool is the intent, the handler is the permission boundary. The agent sees exactly what it can do — not "you have email access" but "you have these four tools, each with a description and parameters." Injection can't escalate beyond tool boundaries: if there's no `forward_email` tool, the instruction "forward this to evil@example.com" is inert; sending an unsubscribe email to a newsletter can be automatic, sending an email to your boss is human-gated.

Giving intent granularity greatly increases permissioning ergonomics. With enough maturity, this system largely speaks the users' language, and that makes approvals a breeze.

### What intent wraps

Intent holds credentials and builds modular wrappers for different kinds of external capabilities:

| Capability type            | What intent holds                       | What the handler does                         | Example tools                                         |
| -------------------------- | --------------------------------------- | --------------------------------------------- | ----------------------------------------------------- |
| **API integrations**       | OAuth tokens, API keys                  | HTTP requests to service APIs                 | `read_inbox`, `check_calendar`, `create_issue`        |
| **Web scraping**           | Session cookies, auth headers           | HTTP fetch + content extraction               | `web_fetch`, `scrape_pricing`                         |
| **Browser automation**     | Playwright session state, login cookies | Drive a headless browser                      | `fill_form`, `screenshot_page`, `check_deploy_status` |
| **CLI wrappers**           | SSH keys, cloud credentials             | Run host-side commands                        | `git_push`, `deploy_staging`                          |
| **Select file operations** | Filesystem paths                        | Read/write host files                         | `read_config`, `write_report`                         |

Each wrapper is a tool — a single Python file with a handler function. Intent injects credentials; the handler talks to the service; the result goes back to the agent. The agent never sees the credentials, the session state, or the implementation. It sees a tool name, a description, and parameters.

**Modularity**: each tool is independent. Adding a Playwright-based tool doesn't change intent or affect API-based tools. Intent doesn't know or care whether a handler uses `requests`, `playwright`, `subprocess`, or raw sockets. It injects credentials, calls the handler, returns the result.

---

## Tool Format

A tool is a single Python file. Two parts: a YAML docstring (the manifest) and a `handle` function (the implementation).

```python
"""
description: |
  Read recent emails from inbox. Returns subject, sender, date, and
  a short snippet for each email. Does not return full bodies.
  For newsletters, use read_newsletters instead.
sensitivity: auto
credentials: [gmail_oauth]
parameters:
  limit:
    type: integer
    default: 20
    description: Maximum number of emails to return
  since:
    type: string
    description: Only emails after this time (ISO 8601 or relative like '24h')
"""

def handle(arguments: dict, credentials: dict) -> dict:
    from _lib.gmail import connect

    client = connect(credentials["gmail_oauth"])
    emails = client.fetch_inbox(
        limit=arguments.get("limit", 20),
        since=arguments.get("since"),
    )
    return {"emails": [
        {"from": e.sender, "subject": e.subject, "date": e.date, "snippet": e.snippet}
        for e in emails
    ]}
```

**One file = one tool = one intent.** The file lives in `tools/` with the tool name as filename: `tools/read_inbox.py`.

### The format tradeoff

This is a deliberate middle ground between two extremes:

| | Pure skill.md | JSON manifest + script | This design |
|---|---|---|---|
| Files per tool | 1 (markdown) | 2 (JSON + script) | 1 (Python) |
| Metadata format | YAML frontmatter | Strict JSON Schema | YAML docstring |
| Handler | None (declarative) | Separate executable | `handle` function |
| Rich descriptions | Natural (it's prose) | Awkward (JSON strings) | Natural (YAML multiline) |
| Programmatic parsing | Parse YAML header | Parse JSON | Parse YAML from docstring |
| Agent can create one | Write a markdown file | Write two files, match schema | Write one Python file |
| Agent can read one | Read one file | Read two files, correlate | Read one file |

The skill.md approach is great for declarative prompts but can't express executable handlers. The JSON + script approach is precise but splits a single concept across two files and makes descriptions painful. The YAML-docstring-in-Python approach keeps everything in one file, makes descriptions natural, and gives you executable code.

### Manifest fields

| Field | Required | Type | Meaning |
|---|---|---|---|
| `description` | yes | string | What this tool does, sent to the agent verbatim. Be specific — see "Writing descriptions" below. |
| `sensitivity` | no | `low` \| `medium` \| `high` | Risk classification. Default: `low`. Logged in audit entries; `high` triggers WARNING-level log. |
| `credentials` | no | list[string] | Keys to inject from `secrets.json`. Omit if no credentials needed. |
| `parameters` | no | mapping | Parameter definitions. Intent expands to JSON Schema. |
| `timeout` | no | integer | Execution timeout in seconds. Default: 30. |
| `group` | no | string | Optional grouping metadata for harness-side tool selection. Default: `""`. |

The tool name is derived from the filename (minus `.py`). No `name` field in the manifest.

### Parameter shorthand

Parameters use compact YAML. Intent expands to JSON Schema:

```yaml
# What you write:
parameters:
  query: {type: string, required: true, description: Search terms}
  limit: {type: integer, default: 10}

# What intent expands to:
{
  "type": "object",
  "properties": {
    "query": {"type": "string", "description": "Search terms"},
    "limit": {"type": "integer", "default": 10}
  },
  "required": ["query"]
}
```

`required: true` on a parameter moves it to the `required` array. Everything else passes through as JSON Schema properties.

### The handle function

Always the same signature:

```python
def handle(arguments: dict, credentials: dict) -> dict:
```

- `arguments`: validated by intent against the parameter schema before the handler is called. The handler can trust the types.
- `credentials`: only the keys listed in the manifest's `credentials` field. Empty dict if no credentials declared.
- Returns: a JSON-serializable dict. This becomes the tool result the agent sees.
- Errors: raise any exception. Intent catches it and returns the error message to the agent.

### Writing descriptions

The description is the primary interface between the tool and the agent. Write it as if you're telling a colleague what this tool does.

**Good description:**
```yaml
description: |
  Read recent emails from inbox. Returns subject, sender, date, and snippet.
  Does NOT return full email bodies — use read_email_body for that.
  For newsletters, use read_newsletters instead (auto-filters and summarizes).
```

**Bad description:**
```yaml
description: Email reading tool
```

Include: what it returns, what it doesn't do, when to use a different tool instead. The agent makes tool selection decisions based on descriptions. Vague descriptions lead to wrong tool choices.

---

## Architecture

### Directory layout

```
intent/
  intent/
    __init__.py
    __main__.py      # Entry point, argparse, Starlette app, SIGHUP handler
    config.py        # Config dataclass, secrets validation, per-call scoped reads
    auth.py          # Token gen/load, ASGI middleware, constant-time compare
    registry.py      # ast.parse manifest extraction, YAML parse, schema expansion
    dispatch.py      # GET /tools, POST /tools/{name}/call, credential scoping
    audit.py         # JSONL append-only log with sensitivity level
    pool.py          # Process pool — one persistent worker per tool via multiprocessing
  tools/
    _lib/            # Shared Python helpers (underscore = not a tool)
    echo.py          # Test tool
  pyproject.toml
  .gitignore
  secrets.json       # Not in git, mode 0600
  audit.jsonl        # Not in git, append-only
```

Intent loads every `.py` file in `tools/` that doesn't start with `_`. Flat directory, no nesting. `ls tools/` shows every tool. An agent can read any tool file and understand it completely.

### Framework: Starlette

Starlette is FastAPI's foundation — raw ASGI, explicit routing, middleware support, zero magic. FastAPI would pull in pydantic, typing extensions, and auto-generated OpenAPI docs that aren't needed. Intent defines its own JSON Schema expansion from YAML manifests, so pydantic models would be redundant.

Dependencies: `starlette`, `uvicorn`, `pyyaml`, `jsonschema`.

### Five responsibilities

Intent does five things. If a proposed change doesn't serve one of these five, it doesn't belong in intent.

1. **Tool registry**: Parse manifests from `tools/*.py` at startup. Use `ast.parse` + `ast.get_docstring` to extract docstrings without executing code, parse as YAML, expand parameter shorthand to JSON Schema, verify `handle()` exists via AST node inspection.

2. **Schema validation**: Validate tool call arguments against JSON Schema before calling the handler.

3. **Credential injection**: Read `secrets.json` from disk per-call (not cached at startup), scoped to only the keys each tool declares. The full secrets dict goes out of scope immediately after filtering.

4. **Tool execution**: Each tool runs in its own persistent subprocess (`multiprocessing.Process` with `spawn`), communicating via `Pipe`. Workers are lazy-spawned on first call and respawn if they crash. `asyncio.wait_for` enforces per-tool timeout; on timeout the worker is killed and respawns on next call.

5. **Audit log**: Append every tool call to a JSONL file — timestamp, tool name, arguments, result summary, duration, error, and sensitivity level. High-sensitivity calls are logged at WARNING level.

### What intent does NOT do

- Agent type filtering (tools are globally available to authenticated callers)
- Approval gating (sensitivity is parsed but not yet enforced)
- Input sanitization (homoglyphs, zero-width characters, Unicode normalization)
- Orchestration (reconstruction, crystallization are separate concerns)
- Index generation (no INDEX.md auto-generation)

### Security boundary stance

Intent is an **intent broker, not a sanitization layer.** It enforces caller authentication, injects credentials, and logs everything. It does not inspect or sanitize the content flowing through tool results.

The `tools/` directory is a **trusted directory** — only place tool files authored or reviewed by the operator. While intent uses `ast.parse` (no code execution) at startup to extract manifests, tool code does execute in worker subprocesses when called. Process isolation prevents a tool handler from accessing intent's memory, credential state, or other tools' workers.

Defenses against prompt injection, homoglyph attacks, and other content-level threats are the responsibility of the consuming agent harness. Intent is designed to be used inside such a harness. If deployed in a weaker harness, tool handlers can implement their own content sanitization — but intent's core does not assume this burden. Keeping it out is what keeps intent at ~430 LOC and auditable.

### Tool loading (startup)

For each `.py` file in `tools/` (excluding `_`-prefixed):

1. Read the file as text
2. `ast.parse` the source — no code execution
3. `ast.get_docstring` extracts the module docstring
4. Parse the docstring as YAML → manifest
5. Expand parameter shorthand to JSON Schema
6. Verify `handle()` exists via AST node inspection (rejects `async` handlers with a clear error)
7. Store the resolved file path; register in the tool map: `{name → ToolManifest}`

No tool code executes at startup. Tool modules are loaded via `importlib` inside worker subprocesses on first call.

### Executing tools

```
Harness → POST /tools/{name}/call → Intent validates schema
                                      → read_scoped_secrets from disk (per-call)
                                      → run_in_executor: pool.call(name, path, args, creds)
                                        → Pipe send to worker subprocess
                                        → worker calls handle(args, creds)
                                        → Pipe recv result
                                      → asyncio.wait_for enforces timeout
                                      → Intent logs to audit (with sensitivity)
                                      → HTTP 200 {"result": ...}
```

Each tool runs in its own persistent subprocess (`multiprocessing.Process` with `spawn` start method). The worker loads the tool module once via `importlib`, then loops receiving calls over a `Pipe`. A `threading.Lock` serializes concurrent requests to the same tool (Pipe is not thread-safe). On timeout, the worker is killed and respawns lazily on next call.

### Size target

~430 LOC total (pool.py accounts for the increase over the original ~280 target).

| Component | LOC |
|---|---|
| Entry point + config | ~75 |
| Auth (token + middleware) | ~45 |
| Registry (load, parse, schema) | ~95 |
| Dispatch (list, call, timeout) | ~60 |
| Process pool | ~100 |
| Audit log | ~25 |
| **Total** | **~400** |

The server is the security boundary — if it's growing past 500 LOC, something belongs in a different component.

---

## Caller Authentication

Two layers, both active by default:

1. **Unix domain socket** — uvicorn binds a UDS at `/run/user/$UID/intent-<random>.sock` (mode 0600). No TCP listener, no port scanning, no network-level access. Socket path is randomized per session. Filesystem permissions restrict connections to the owning user.

2. **Bearer token** — `secrets.token_urlsafe(32)`, generated in memory per session. ASGI middleware checks `Authorization: Bearer <token>` with `hmac.compare_digest()` (constant-time, stdlib). Every request except non-HTTP ASGI scopes must include the token.

Token lifecycle:
- On startup, intent generates a fresh token in memory.
- Intent prints a single machine-readable line to stdout: `INTENT_TOKEN=<value> INTENT_SOCK=<path>`.
- The harness (parent process) captures this from intent's stdout.
- No token file is written by default. Use `--token-file <path>` to opt into file-based token storage for debugging.

TCP fallback (opt-in, less secure):
- `--tcp` switches to TCP on `127.0.0.1:7400` and defaults `--token-file` to `./token`.
- A warning is logged that the token file is readable by any same-user process.

---

## Protocol

HTTP over Unix domain socket. Two endpoints.

### GET /tools

```bash
# UDS (default) — capture INTENT_TOKEN and INTENT_SOCK from intent's stdout
curl --unix-socket $INTENT_SOCK http://localhost/tools \
  -H "Authorization: Bearer $INTENT_TOKEN"

# TCP fallback
curl http://127.0.0.1:7400/tools \
  -H "Authorization: Bearer $(cat token)"
```

```json
{
  "tools": [
    {
      "name": "read_inbox",
      "description": "Read recent emails from inbox...",
      "parameters": {"type": "object", "properties": {...}, "required": [...]}
    }
  ]
}
```

Returns all loaded tools. The response uses standard JSON Schema for parameters. The harness can translate to whatever format its LLM expects.

### POST /tools/{name}/call

```bash
# UDS (default)
curl --unix-socket $INTENT_SOCK -X POST http://localhost/tools/read_inbox/call \
  -H "Authorization: Bearer $INTENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"arguments": {"limit": 5}}'

# TCP fallback
curl -X POST http://127.0.0.1:7400/tools/read_inbox/call \
  -H "Authorization: Bearer $(cat token)" \
  -H "Content-Type: application/json" \
  -d '{"arguments": {"limit": 5}}'
```

Success:
```json
{"result": {"emails": [...]}}
```

Errors:
```json
{"error": "tool not found: read_inbox"}
{"error": "validation: 'query' is a required property"}
{"error": "tool timed out after 30s"}
{"error": "ConnectionError: ..."}
```

### Error format

All errors use the same shape:
```json
{"error": "Human-readable error message"}
```

HTTP status codes: 200 success, 400 validation, 401 auth, 404 unknown tool, 504 timeout, 500 handler failure.

---

## Credential Flow

```
secrets.json (host, mode 0600)
    │
    │  read from disk per-call (not cached in memory)
    │  scoped to tool's declared credential keys
    ▼
handle(arguments, credentials)  ← only declared keys, in worker subprocess
    │
    │  handler calls external service
    ▼
Handler returns result → Pipe → HTTP response (credentials never in result)
```

**Scoping rule:** A tool that declares `credentials: [gmail_oauth]` receives only that key, even if `secrets.json` contains credentials for 20 services. Intent reads the file per-call, filters to declared keys, and the full dict goes out of scope immediately. A handler cannot request credentials it didn't declare.

**The agent never sees credentials.** Credentials flow from secrets file → scoped dict → Pipe → worker subprocess → handler function arguments. The handler's return value is the tool result, which goes back to the agent. Credentials never appear in the result. No credentials are held resident in memory between calls.

---

## Audit Log Format

Every tool call produces one JSONL entry:

```json
{
  "ts": "2026-03-28T14:30:00Z",
  "tool": "read_inbox",
  "args": {"limit": 10, "since": "24h"},
  "result_summary": "{'emails': [...]}",
  "duration_ms": 1240,
  "error": "",
  "sensitivity": "low"
}
```

| Field | Type | Meaning |
|---|---|---|
| `ts` | ISO 8601 | When the call completed |
| `tool` | string | Tool called |
| `args` | object | Arguments (full, not summarized) |
| `result_summary` | string | First 200 chars of serialized result |
| `error` | string | Error message if the call failed, empty string otherwise |
| `duration_ms` | integer | Wall-clock handler execution time |
| `sensitivity` | string | Tool's sensitivity level (`low`, `medium`, `high`) |

The audit log is append-only, opened with `O_APPEND`, and never modified after writing. Mode 0600.

---

## Creating Tools

### Workflow

1. Create `tools/my_tool.py` — YAML docstring + `handle` function
2. Restart intent (or send SIGHUP for live reload)

One file, one function. That's it.

### What makes a good tool

**Specific, not generic.** `read_newsletters` not `read_email(filter="newsletters")`. If you're adding a parameter to select between fundamentally different behaviors, you want separate tools. The tool name should describe the intent completely.

**One intent, one sensitivity level.** Read operations are low-risk. Write operations are higher. If a tool does both, split it. Intent granularity is what makes sensitivity levels ergonomic: `send_email_low_risk` (pre-approved recipients) and `send_email` (any recipient) share a service but have different risk profiles.

**Narrow return data.** `read_inbox` returns subjects and snippets, not full bodies. `check_calendar` returns event titles and times, not attendee lists and meeting notes. Return the minimum the agent needs. Narrower returns mean less data at risk if the agent is compromised, and less noise in the agent's context.

**Clear hierarchy.** Tools form intent hierarchies — narrow tools are subsets of broader tools:

```
read_email (broad — all mail, full bodies, search)
  ├── read_inbox (medium — recent mail, subjects + snippets only)
  ├── read_newsletters (narrow — newsletters only, summarized)
  └── read_github_updates (narrow — GitHub notification emails only)

send_email (broad — any recipient)
  ├── send_email_low_risk (narrow — pre-approved recipients)
  └── unsubscribe_newsletter (narrow — sends unsubscribe)
```

**The consumer agent should always prefer the narrowest tool that accomplishes the task.** This is a convention enforced through tool descriptions and the agent's system prompt, not by intent. When multiple tools can accomplish a task, the narrowest tool requires the least trust and exposes the least data. Tool descriptions should guide this: "For newsletters, use `read_newsletters` instead" in `read_inbox`'s description tells the agent to reach for the narrow tool first.

**Description tells the agent when NOT to use it.** "For newsletters, use `read_newsletters` instead" is more useful than listing everything the tool does. The agent's hardest decision is choosing between similar tools at different levels of the hierarchy — descriptions are how you guide that choice.

---

## Live Reload

SIGHUP triggers a full reload. The signal handler kills all worker subprocesses (`pool.shutdown()`), then calls `registry.load(tools_dir)`, which re-scans `tools/*.py` and parses manifests via `ast.parse`. Workers respawn lazily on the next call to each tool, loading the updated code. In-flight requests continue with old worker references; new requests use the updated registry.

```bash
kill -HUP $(pgrep -f "python -m intent")
```

---

## CLI

```bash
python -m intent [options]
```

| Flag | Default | Meaning |
|---|---|---|
| `--uds` | auto-generated | Explicit Unix domain socket path |
| `--tcp` | off | Listen on TCP instead of UDS (less secure) |
| `--bind` | `127.0.0.1` | Listen address (TCP mode only) |
| `--port` | `7400` | Listen port (TCP mode only) |
| `--tools-dir` | `tools` | Directory to scan for tool files |
| `--secrets` | `secrets.json` | Path to credentials file |
| `--audit` | `audit.jsonl` | Path to audit log |
| `--token-file` | `None` | Write token to file (default: stdout only; `--tcp` defaults to `token`) |

---

## Examples: Broad vs. Narrow Tools

These examples show the intent hierarchy in action. Each pair shares the same underlying service but differs in scope, return data, and sensitivity.

### Narrow read tool (restricted scope)

`tools/read_newsletters.py`:
```python
"""
description: |
  Read newsletter emails only. Filters to known newsletter senders,
  returns summaries (not full bodies). Use this for morning digests
  and newsletter triage.
  For all inbox mail, use read_inbox instead.
sensitivity: low
credentials: [gmail_oauth]
parameters:
  limit:
    type: integer
    default: 10
    description: Maximum newsletters to return
  since:
    type: string
    default: "24h"
    description: Only newsletters after this time
"""

def handle(arguments: dict, credentials: dict) -> dict:
    from _lib.gmail import connect, NEWSLETTER_SENDERS

    client = connect(credentials["gmail_oauth"])
    emails = client.fetch_inbox(
        limit=arguments.get("limit", 10),
        since=arguments.get("since", "24h"),
        from_filter=NEWSLETTER_SENDERS,
    )
    return {"newsletters": [
        {"from": e.sender, "subject": e.subject, "summary": e.snippet[:200]}
        for e in emails
    ]}
```

### Broad read tool (wider scope)

`tools/read_inbox.py`:
```python
"""
description: |
  Read recent emails from inbox. Returns subject, sender, date, and
  snippet for each email. Does NOT return full bodies.
  For newsletters only, prefer read_newsletters (narrower, pre-filtered).
  For full email bodies, use read_email_body.
sensitivity: low
credentials: [gmail_oauth]
parameters:
  limit:
    type: integer
    default: 20
    description: Maximum number of emails to return
  since:
    type: string
    description: Only emails after this time (ISO 8601 or relative like '24h')
"""

def handle(arguments: dict, credentials: dict) -> dict:
    from _lib.gmail import connect

    client = connect(credentials["gmail_oauth"])
    emails = client.fetch_inbox(
        limit=arguments.get("limit", 20),
        since=arguments.get("since"),
    )
    return {"emails": [
        {"from": e.sender, "subject": e.subject, "date": e.date, "snippet": e.snippet}
        for e in emails
    ]}
```

### Narrow send tool (pre-approved recipients)

`tools/send_email_low_risk.py`:
```python
"""
description: |
  Send an email to a pre-approved recipient. The recipient must be in the
  approved list (e.g., unsubscribe addresses, automated systems, known
  contacts marked as low-risk).
  For sending to any recipient, use send_email instead.
sensitivity: low
credentials: [gmail_oauth]
parameters:
  to:
    type: string
    required: true
    description: Recipient (must be in pre-approved list)
  subject:
    type: string
    required: true
    description: Email subject line
  body:
    type: string
    required: true
    description: Email body (plain text)
"""

def handle(arguments: dict, credentials: dict) -> dict:
    from _lib.gmail import connect, APPROVED_RECIPIENTS

    if arguments["to"] not in APPROVED_RECIPIENTS:
        raise ValueError(
            f"Recipient {arguments['to']} is not pre-approved. "
            f"Use send_email instead."
        )
    client = connect(credentials["gmail_oauth"])
    msg_id = client.send(
        to=arguments["to"],
        subject=arguments["subject"],
        body=arguments["body"],
    )
    return {"sent": True, "message_id": msg_id}
```

### Broad send tool (any recipient)

`tools/send_email.py`:
```python
"""
description: |
  Send an email to any recipient. Higher sensitivity — consider whether
  send_email_low_risk can accomplish the task instead.
sensitivity: high
credentials: [gmail_oauth]
parameters:
  to:
    type: string
    required: true
    description: Recipient email address
  subject:
    type: string
    required: true
    description: Email subject line
  body:
    type: string
    required: true
    description: Email body (plain text)
"""

def handle(arguments: dict, credentials: dict) -> dict:
    from _lib.gmail import connect

    client = connect(credentials["gmail_oauth"])
    msg_id = client.send(
        to=arguments["to"],
        subject=arguments["subject"],
        body=arguments["body"],
    )
    return {"sent": True, "message_id": msg_id}
```

Notice the pattern: both send tools share `_lib.gmail` but have different scopes. The narrow tool validates against an approved list and has `low` sensitivity. The broad tool sends to anyone with `high` sensitivity. The descriptions cross-reference each other so the agent knows when to prefer the narrow tool.

---

## Decisions

- **Live reload**: SIGHUP. Rescans the tools directory and replaces the registry. No file-watching.

- **Handler language**: Python only. Python is great glue — any external library or service can be wrapped in a Python handler. The single-file format's value comes from the unified format; adding language variants breaks that.

- **Large results**: Pagination by default. Tool results include a banner summarizing total size; the agent receives one page and can request more by calling the tool again with a `page` or `offset` parameter. Intent doesn't handle pagination — the tool handler does. This keeps intent simple and lets each tool define sensible page sizes for its data type.

- **Tool testing**: `handle` is a regular Python function — testable directly with `handle(args, creds)`. No special harness needed.

---

## Future Work

- **Agent type filtering** — Agent types (defined externally) reference tools by name. Intent would filter `GET /tools` and reject `POST /tools/{name}/call` for tools not in the caller's type. Not yet implemented; all tools are available to any authenticated caller.

- **Approval gate** — For high-sensitivity tools, block the HTTP response until a human approves on the host terminal. Sensitivity is parsed from manifests but not yet enforced.

- **HTTPS** — For non-localhost deployments.

- **Rate limiting** — Per-tool or global request throttling.

---

## Sources

- Claude Code skill format — inspiration for single-file definitions with YAML frontmatter
- OpenAI function calling / Claude tool use — JSON Schema parameter format, tool description conventions
- CaMeL (DeepMind, 2025) — capability-based security, tool-as-permission-boundary concept
- RBAC literature (NIST SP 800-207, Zero Trust Architecture) — role-based model this design departs from
