# Design Doc: Proxy and Tool System

## Purpose

Spec for the proxy — a **standalone tool server** that any agent harness can consume. The proxy holds API keys, OAuth tokens, and session state. It wraps each external capability as a narrow, named intent and exposes it over HTTP as a callable tool. That's the entire abstraction.

The proxy is harness-agnostic. It doesn't know about Claude, OpenAI, LangChain, or any specific agent framework. It serves tools over HTTP. Any harness that can make HTTP requests can use it — our own harness, Hermes, or anything else.

This document is designed to be read in full by both humans and agents. A 200k-context model should be able to hold the entire proxy design (this doc + the ~400 LOC implementation) in working memory. If it can't, the design is too complex.

Related: `agent-security-design-doc.md` (harness architecture that consumes this proxy), `context-reconstruction-design.md` (context management).

---

## Why Not MCP

MCP (Model Context Protocol) solves discovery and transport across trust boundaries: different tool authors, different hosts, dynamic capability negotiation, schema evolution, server lifecycle management.

This proxy is simpler by design. It's a tool server, not an interop protocol:

| MCP feature                                   | This proxy                                                                           |
| --------------------------------------------- | ------------------------------------------------------------------------------------ |
| Capability negotiation                        | Agent type YAML declares tools. Static, known at startup.                            |
| Transport abstraction (stdio, SSE, HTTP)      | HTTP on localhost. One transport.                                                    |
| Server lifecycle (initialize, ping, shutdown) | Start with `harness proxy`, stop with Ctrl-C.                                        |
| Schema evolution / versioning                 | Change the schema, restart. No migration.                                            |
| Resource/prompt primitives                    | Tools are the only primitive.                                                        |
| JSON-RPC 2.0 compliance                       | Two REST endpoints. No batch, no notifications.                                      |

**Two HTTP endpoints.** `GET /tools` and `POST /tools/{name}/call`. Any language, any framework, any agent harness can call them. Debug with curl. No custom client library needed.

MCP would work here — it's not wrong. But it's ~5x the protocol surface for zero benefit when the proxy is a standalone tool server. The complexity cost compounds: every consuming harness needs an MCP client, the proxy needs the full MCP server spec, debugging requires MCP-aware tooling. HTTP is the universal interface.

---

## Core Concept: Tools as Intents

The proxy bridges role-based access control to intent-based access control. The bridge is **tool decomposition**.

| | RBAC (traditional) | Intent-based (this design) |
|---|---|---|
| Identity | Role: `email-manager` | Agent type: `email-manager` |
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

### What the proxy wraps

The proxy holds credentials and builds modular wrappers for different kinds of external capabilities:

| Capability type            | What the proxy holds                    | What the handler does                         | Example tools                                         |
| -------------------------- | --------------------------------------- | --------------------------------------------- | ----------------------------------------------------- |
| **API integrations**       | OAuth tokens, API keys                  | HTTP requests to service APIs                 | `read_inbox`, `check_calendar`, `create_issue`        |
| **Web scraping**           | Session cookies, auth headers           | HTTP fetch + content extraction               | `web_fetch`, `scrape_pricing`                         |
| **Browser automation**     | Playwright session state, login cookies | Drive a headless browser                      | `fill_form`, `screenshot_page`, `check_deploy_status` |
| **CLI wrappers**           | SSH keys, cloud credentials             | Run host-side commands                        | `git_push`, `deploy_staging`                          |
| **Select file operations** | Filesystem paths outside container      | Read/write host files the container can't see | `read_config`, `write_report`                         |

Each wrapper is a tool — a single Python file with a handler function. The proxy injects credentials; the handler talks to the service; the result goes back to the agent. The agent never sees the credentials, the session state, or the implementation. It sees a tool name, a description, and parameters.

**Modularity**: each tool is independent. Adding a Playwright-based tool doesn't change the proxy or affect API-based tools. The proxy doesn't know or care whether a handler uses `requests`, `playwright`, `subprocess`, or raw sockets. It injects credentials, calls the handler, returns the result.

---

## Tool Format

A tool is a single Python file. Two parts: a YAML docstring (the manifest) and a `handle` function (the implementation).

```python
"""
name: read_inbox
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
| `name` | yes | string | Tool name. Must match filename (minus `.py`). Lowercase + underscores. |
| `description` | yes | string | What this tool does, sent to the agent verbatim. Be specific — see "Writing descriptions" below. |
| `sensitivity` | yes | `auto` \| `approve` | `auto`: executes without prompting. `approve`: blocks for human yes/no. |
| `credentials` | no | list[string] | Keys to inject from `secrets.json`. Omit if no credentials needed. |
| `parameters` | no | mapping | Parameter definitions. The proxy expands to JSON Schema. |
| `timeout` | no | integer | Execution timeout in seconds. Default: 60. |

### Parameter shorthand

Parameters use compact YAML. The proxy expands to JSON Schema for the Claude tool format:

```yaml
# What you write:
parameters:
  query: {type: string, required: true, description: Search terms}
  limit: {type: integer, default: 10}

# What the proxy expands to:
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

- `arguments`: validated by the proxy against the parameter schema before the handler is called. The handler can trust the types.
- `credentials`: only the keys listed in the manifest's `credentials` field. Empty dict if no credentials declared.
- Returns: a JSON-serializable dict. This becomes the tool result the agent sees.
- Errors: raise any exception. The proxy catches it and returns the error message to the agent.

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

### Directory layout

```
harness/
  INDEX.md                  # Central index — the agent reads this first
  CONVENTIONS.md            # Tool format, handle signature, how to add a tool
  proxy/
    server.py               # The proxy (~500 LOC, the only core file)
    _runner.py              # Subprocess tool executor (~15 LOC)
  tools/
    read_inbox.py           # one tool = one file
    read_newsletters.py     # narrow: newsletters only
    draft_response.py
    send_email.py           # broad: any recipient, approve
    send_email_low_risk.py  # narrow: pre-approved recipients, auto
    web_fetch.py
    _lib/                   # shared helpers (underscore = not a tool)
      gmail.py
      http.py
  agent_types/
    email-manager.yaml
    researcher.yaml
    architect.yaml
  secrets.json              # credentials (mode 0600, never mounted in containers)
  audit.jsonl               # append-only tool call log
```

**Navigation rule:** An agent touching the codebase reads `INDEX.md` first. The index tells it where everything is, what every tool does (one line each), and which file to open. The agent loads only the file it needs to touch.

The proxy loads every `.py` file in `tools/` that doesn't start with `_`. Flat directory, no nesting. `ls tools/` shows every tool. An agent can read any tool file and understand it completely.

---

## Proxy Architecture

### Five responsibilities

The proxy does five things. If a proposed change doesn't serve one of these five, it doesn't belong in the proxy.

1. **Tool registry**: Parse manifests from `tools/*.py` at startup. Build the tool map.

2. **Schema validation**: Validate tool call arguments against JSON Schema. Reject calls to tools not in the calling agent type's tool list.

3. **Credential injection**: Read `secrets.json` (mode 0600, outside container mounts), inject only the credentials each tool declares.

4. **Approval gate**: For `approve`-sensitivity tools, display the tool name and arguments on the host terminal. Block until the user types yes or no.

5. **Audit log**: Append every tool call to a JSONL file — timestamp, agent type, tool name, arguments, result summary, approval decision, duration. The context reconstructor and crystallization system read from this log.

### What the proxy does NOT do

- Bash validation
- Boundary markers or source tagging (the audit log records provenance)
- Egress gating (individual tools can implement their own if needed)
- Fact extraction (the agent self-reports during memory flush)
- Orchestration (reconstruction, crystallization are separate processes)
- **Input sanitization** (homoglyphs, zero-width characters, Unicode normalization)

### Security boundary stance

The proxy is an **intent broker, not a sanitization layer.** It enforces which tools the agent can call, injects credentials, gates approvals, and logs everything. It does not inspect or sanitize the content flowing through tool results.

Defenses against prompt injection, homoglyph attacks, zero-width character steganography, and other content-level threats are the responsibility of the consuming agent harness — the architecture described in `agent-security-design-doc.md`, which provides `--network none` containers, memory injection scanning, and context reconstruction.

The proxy is designed to be used inside that harness. If deployed in a weaker harness (no container isolation, weaker injection defenses), the proxy supports extension: tool handlers can implement their own content sanitization, and middleware hooks can be added to the tool dispatch pipeline without modifying the core proxy. But the core proxy does not assume this burden — keeping it out is what keeps the proxy at ~500 LOC and auditable.

### Loading tools (startup)

For each `.py` file in `tools/` (excluding `_`-prefixed):

1. Read the file as text (no import, no execution)
2. Parse the docstring using Python's `ast` module: `ast.parse(text)` → `ast.get_docstring(tree)`
3. Parse the docstring content as YAML → manifest
4. Validate required fields (name, description, sensitivity)
5. Expand parameter shorthand to JSON Schema
6. Register in the tool map: `{name → (manifest, file_path)}`

The `ast` module parses Python syntax without executing it. No top-level code runs at load time. Handlers are only executed when the tool is called.

At startup, the proxy also regenerates `INDEX.md` from the loaded manifests and agent type YAMLs. The index is always in sync with the actual tools on disk.

### Executing tools

```
Harness → POST /tools/{name}/call → Proxy validates schema + permissions
                                      → Proxy injects credentials
                                      → Subprocess: python _runner.py <tool_path>
                                           stdin:  {"arguments": {...}, "credentials": {...}}
                                           stdout: {"result": ...}
                                      → Proxy logs to audit
                                      → HTTP 200 {"result": ...}
```

The proxy includes a small runner script (`_runner.py`, ~15 LOC) that imports the tool module and calls `handle`. The tool runs in an isolated subprocess — a buggy handler can't crash or corrupt the proxy.

### Size target

~400 LOC total. Smaller than before — removing `call_llm` and using a standard HTTP framework (Flask/Starlette/bare `http.server`) instead of a custom UDS protocol saves code.

| Component | LOC |
|---|---|
| HTTP server + routing | ~50 |
| Tool loading (ast parse, YAML, schema expansion) | ~60 |
| Dispatch (GET /tools, POST /tools/{name}/call) | ~50 |
| Schema validation | ~40 |
| Credential injection | ~20 |
| Approval gate | ~30 |
| Audit log | ~40 |
| Subprocess runner | ~15 |
| Startup + config | ~40 |
| **Total** | **~345** |

Headroom for edge cases. The proxy is the security boundary — if it's growing past 400 LOC, something belongs in a different component.

---

## Protocol

HTTP on localhost. Two endpoints.

### GET /tools

```bash
curl http://localhost:7400/tools?agent_type=email-manager
```

```json
[
  {
    "name": "read_inbox",
    "description": "Read recent emails from inbox...",
    "input_schema": {"type": "object", "properties": {...}}
  },
  {
    "name": "read_newsletters",
    "description": "Read newsletters only — filtered, summarized...",
    "input_schema": {"type": "object", "properties": {...}}
  }
]
```

Returns only the tools granted to the specified agent type. The response uses the standard tool schema (compatible with Claude, OpenAI, and other tool-use APIs). The harness can pass it directly to its LLM.

### POST /tools/{name}/call

```bash
curl -X POST http://localhost:7400/tools/read_inbox/call \
  -H "Content-Type: application/json" \
  -d '{"agent_type": "email-manager", "arguments": {"limit": 5}}'
```

```json
{"result": {"emails": [...]}}
```

On error:
```json
{"error": "Unknown tool: read_inbox"}
{"error": "Missing required parameter: query"}
{"error": "Tool read_inbox not available to agent type: morning-digest"}
```

For `approve`-sensitivity tools, the proxy blocks the HTTP response until the user responds on the host terminal:
```
[APPROVAL REQUIRED] send_email (agent: email-admin)
  to: bob@example.com
  subject: Re: Meeting
  body: "Sounds good, see you at 3pm."
  Approve? [y/N] █
```

If denied:
```json
{"error": "User denied: send_email"}
```

### What about call_llm?

**Not in the proxy.** The proxy is a tool server, not an LLM relay. Each consuming harness talks to its own LLM its own way — Claude API, OpenAI API, local models, whatever. The proxy doesn't need to know.

For this harness specifically (where the agent loop runs on the host), the harness calls the Claude API directly and calls the proxy for tools. Other harnesses do whatever they do. The proxy serves tools; the harness handles everything else.

### Error format

All errors use the same shape:
```json
{"error": "Human-readable error message"}
```

HTTP status codes: 200 for success, 400 for validation errors, 403 for permission/denial errors, 500 for handler failures. The error message is what matters — status codes are for HTTP clients that need them.

### UDS as optional transport

For co-located harnesses that want sub-millisecond latency, the proxy can optionally listen on a UDS socket in addition to HTTP. Same JSON request/response format, different transport. Configure via `--uds /path/to/socket`. The default is HTTP-only.

---

## Credential Flow

```
secrets.json (host, mode 0600)
    │
    │  proxy reads at startup, holds in memory
    ▼
Proxy credential store (in-memory dict)
    │
    │  on POST /tools/{name}/call: filter to tool's declared credential keys
    ▼
Subprocess stdin: {"arguments": {...}, "credentials": {"gmail_oauth": {...}}}
    │
    │  handler reads credentials, calls external service
    ▼
Handler returns result → HTTP response (credentials never in result)
```

**Scoping rule:** A tool that declares `credentials: [gmail_oauth]` receives only that key, even if `secrets.json` contains credentials for 20 services. The proxy filters before injection. A handler cannot request credentials it didn't declare.

**The agent never sees credentials.** The container has no access to `secrets.json` (it's not mounted). Credentials flow from secrets file → proxy memory → subprocess stdin → handler function. The handler's return value is the tool result, which goes back to the agent. Credentials never appear in the result.

---

## Creating Tools

### Human workflow

1. Create `tools/my_tool.py` (reference CONVENTIONS.md for the format)
2. Write the YAML docstring (name, description, sensitivity, parameters)
3. Write the `handle` function
4. Add the tool name to the relevant agent type's `tools` list in its YAML
5. Run `harness index` to regenerate INDEX.md
6. Restart the proxy (or SIGHUP for live reload)

One file, one function, one line in the agent type config, one index command.

### Architect agent workflow

The architect agent reads INDEX.md + CONVENTIONS.md (~700 tokens), then:

1. User asks: "I want the email agent to be able to check my calendar too"
2. Architect reads one existing tool file as a template (e.g., `tools/read_inbox.py`)
3. Architect writes `tools/check_calendar.py.proposed`
4. Architect writes `agent_types/email-manager.yaml.proposed` (adds `check_calendar` to tools list)
5. User reviews the proposed files
6. User activates: rename `.proposed` files to drop the extension
7. Proxy picks up the new tool on restart/SIGHUP; `harness index` regenerates INDEX.md

The `.proposed` extension is the gate. The proxy ignores it. The user decides when to activate. The architect can propose but not deploy.

Total context the architect needs: INDEX.md (300 tokens) + CONVENTIONS.md (400 tokens) + one template tool (300 tokens) = **1,000 tokens.** The architect uses 0.5% of its context to create a tool, leaving 99.5% for reasoning about what the tool should do.

### What makes a good tool

**Specific, not generic.** `read_newsletters` not `read_email(filter="newsletters")`. If you're adding a parameter to select between fundamentally different behaviors, you want separate tools. The tool name should describe the intent completely.

**One intent, one sensitivity level.** Read operations are `auto`. Write operations are `approve`. If a tool does both, split it. Mixing them forces the wrong sensitivity level on one operation. Intent granularity is what makes sensitivity levels ergonomic: `send_email_low_risk` (auto, pre-approved recipients) and `send_email` (approve, any recipient) share a service but have different risk profiles and different approval gates.

**Narrow return data.** `read_inbox` returns subjects and snippets, not full bodies. `check_calendar` returns event titles and times, not attendee lists and meeting notes. Return the minimum the agent needs. Narrower returns mean less data at risk if the agent is compromised, and less noise in the agent's context.

**Clear hierarchy.** Tools form intent hierarchies — narrow tools are subsets of broader tools:

```
read_email (broad — all mail, full bodies, search)
  ├── read_inbox (medium — recent mail, subjects + snippets only)
  ├── read_newsletters (narrow — newsletters only, summarized)
  └── read_github_updates (narrow — GitHub notification emails only)

send_email (broad — any recipient, requires approval)
  ├── send_email_low_risk (narrow — pre-approved recipients, auto)
  └── unsubscribe_newsletter (narrow — sends unsubscribe, auto)
```

Different agent types get different levels of the hierarchy. A morning-digest agent gets only `read_newsletters`. An email-manager gets `read_inbox` + `draft_response` + `send_email_low_risk`. An email-admin gets the broad tools. Matching the tool to the task's actual scope is what makes the permission model work without friction.

**The consumer agent should always prefer the narrowest tool that accomplishes the task.** This is a convention enforced through tool descriptions and the agent's system prompt, not by the proxy. When multiple tools can accomplish a task, the narrowest tool requires the least trust, triggers the least approval friction, and exposes the least data. Tool descriptions should guide this: "For newsletters, use `read_newsletters` instead" in `read_inbox`'s description tells the agent to reach for the narrow tool first.

**Description tells the agent when NOT to use it.** "For newsletters, use `read_newsletters` instead" is more useful than listing everything the tool does. The agent's hardest decision is choosing between similar tools at different levels of the hierarchy — descriptions are how you guide that choice.

---

## Design for Agent Comprehensibility

Design constraint: **the proxy core + the tool the agent is currently working on should take ~10% or less of the agent's context window.** This must hold for a 200k model — the smallest context window we target.

The agent should never need to load the full codebase. It reads an index, navigates to the one file it needs, and has 90%+ of its context left for actual work.

Without an "architect" agent to build the tool registry, this design is unusably complex.

### The 10% budget

10% of 200k = 20,000 tokens. Here's what different working sets cost:

| Scenario | What's loaded | Tokens | % of 200k |
|---|---|---|---|
| **Working on a tool** | INDEX.md + CONVENTIONS.md + one tool file | ~1,000 | 0.5% |
| **Creating a new tool** | INDEX.md + CONVENTIONS.md + one example tool | ~1,000 | 0.5% |
| **Debugging proxy dispatch** | INDEX.md + proxy/server.py + one tool file | ~3,000 | 1.5% |
| **Full system audit** | INDEX.md + CONVENTIONS.md + proxy + all 20 tools + all agent types | ~10,500 | 5.3% |

Even the worst case (full audit of everything) fits in 6%. The normal case (touching one tool) uses 0.5%. The budget is met with wide margins.

### INDEX.md — the central index

The entry point for any agent touching the codebase. Auto-generated by `harness index` from tool manifests and agent type YAMLs. ~300 tokens.

```markdown
# Harness

Standalone tool server over HTTP. Holds credentials, serves tools,
gates approvals, logs everything. Harness-agnostic — any agent
framework that speaks HTTP can consume it.
Default: http://localhost:7400

## Navigation

| Task | Read these files |
|---|---|
| Understand the system | INDEX.md + CONVENTIONS.md |
| Create a new tool | CONVENTIONS.md + one existing tool as template |
| Modify a tool | just that tool's .py file (self-contained) |
| Change agent permissions | just that agent type's .yaml file |
| Debug proxy behavior | proxy/server.py (~500 LOC) |

## Proxy

  proxy/server.py      HTTP tool server, credential injection, approval gate, audit
  proxy/_runner.py      Subprocess tool executor

## Tools

| Tool | Sensitivity | Scope | Description |
|---|---|---|---|
| read_inbox | auto | broad | Read recent emails — subjects, senders, snippets |
| read_newsletters | auto | narrow | Read newsletters only — filtered, summarized |
| draft_response | auto | — | Draft a reply to an email |
| send_email | approve | broad | Send to any recipient (requires approval) |
| send_email_low_risk | auto | narrow | Send to pre-approved recipients only |
| web_fetch | auto | — | Fetch URL, return text content |

## Agent Types

| Type | Tools | Notes |
|---|---|---|
| email-manager | read_inbox, read_newsletters, draft_response, send_email_low_risk | No broad send |
| morning-digest | read_newsletters | Narrowest possible |
| email-admin | read_inbox, draft_response, send_email | Has broad send (approved) |
| researcher | web_fetch, web_search | No email access |
| architect | create_tool, create_agent_type, list_tools, list_types | Meta-tools only |
```

The tool table is the key navigation aid. The agent reads this table, identifies the tool it needs, and opens that one file. It never needs to `ls tools/` and read every file to find what it's looking for.

**Auto-generation:** `harness index` reads every tool manifest (via `ast.get_docstring`, no execution) and every agent type YAML, then writes INDEX.md. Run it after adding/modifying tools. The proxy also regenerates the index at startup.

### CONVENTIONS.md — how to create and modify

The reference for any agent creating or modifying tools. ~400 tokens. Contains the tool format, handle signature, parameter shorthand, naming rules, and the step-by-step workflow for adding a tool.

```markdown
# Conventions

## Tool format

A tool is one Python file in tools/. YAML docstring + handle function.

\"""
name: tool_name          # must match filename (minus .py)
description: |           # what the agent sees — be specific
  What this tool does. What it returns.
  When to use something else instead.
sensitivity: auto        # auto | approve
credentials: [key_name]  # from secrets.json (omit if none)
parameters:
  param_name:
    type: string
    required: true
    description: what this parameter does
\"""

def handle(arguments: dict, credentials: dict) -> dict:
    # arguments: validated by proxy
    # credentials: only declared keys
    # return: JSON-serializable dict
    # errors: raise exception
    return {"result": "..."}

## Adding a tool

1. Create tools/my_tool.py (follow format above)
2. Add tool name to agent type's tools list in its .yaml
3. Restart proxy (or SIGHUP)
4. Run `harness index` to update INDEX.md

## Rules

- One intent per tool. Read ≠ write. Split if mixed.
- Tool name = filename. Lowercase + underscores.
- _ prefix = helper, not a tool. .proposed = staged, not active.
- Descriptions: say what it returns AND when NOT to use it.
- Parameters: {type: string, required: true} shorthand.
```

### Why the index pattern works for agents

The index solves a problem specific to LLM agents: **they can't efficiently scan a directory**. A human runs `ls` and recognizes filenames. An agent has to read each file to understand what it contains — or it reads an index that already summarizes them.

The navigation pattern is always two steps:
1. Read INDEX.md → find the file path
2. Read that one file → do the work

This mirrors how agents already navigate codebases (read a README, then read the specific file). The difference here is that the index is dense, structured, and auto-generated — it's always accurate and always concise.

### Why these specific design choices

| Decision | Alternative considered | Why this wins for agent comprehension |
|---|---|---|
| Central INDEX.md | Agent scans directory | Two reads vs. N reads. The index has what `ls` can't show: descriptions. |
| CONVENTIONS.md separate from design doc | Everything in one doc | Agent creating a tool needs 400 tokens, not 2,500. Load only what's needed. |
| Single file per tool | Manifest + handler in separate files | One read to understand a tool. No cross-file correlation. |
| YAML docstring | JSON manifest | YAML is more readable; multiline descriptions flow naturally. |
| Flat `tools/` directory | Nested directories per tool | Index stays flat. One table row per tool. |
| Tool name = filename | Name in config, arbitrary filename | No indirection. Index path points directly to the tool. |
| Parameter shorthand | Full JSON Schema in manifests | Less noise in the tool file. Keeps tokens low. |
| Three protocol methods | REST, gRPC, MCP | Minimum viable interface. Fits in one screen. |
| `handle(arguments, credentials)` | Varying signatures per tool | One pattern in CONVENTIONS.md, every tool follows it. |
| Auto-generated index | Hand-maintained README | Always accurate. No drift between index and actual tools. |

### What the agent sees at runtime

When the harness calls `GET /tools`, it receives the standard tool format:

```json
[
  {
    "name": "read_inbox",
    "description": "Read recent emails from inbox. Returns subject, sender, date, and snippet...",
    "input_schema": {
      "type": "object",
      "properties": {
        "limit": {"type": "integer", "default": 20, "description": "Maximum number of emails"},
        "since": {"type": "string", "description": "Only emails after this time..."}
      }
    }
  }
]
```

The agent doesn't need to know about the proxy internals, the handler protocol, or the credential flow. It sees tools with descriptions and parameters — the same format it would see from any tool-use API. The harness translates between the proxy's HTTP responses and whatever format its LLM expects. The proxy is invisible to the agent.

---

## Audit Log Format

Every tool call produces one JSONL entry:

```json
{
  "ts": "2026-03-28T14:30:00Z",
  "agent": "email-manager",
  "tool": "read_inbox",
  "args": {"limit": 10, "since": "24h"},
  "result_summary": "returned 7 emails",
  "approved": null,
  "duration_ms": 1240,
  "error": ""
}
```

| Field | Type | Meaning |
|---|---|---|
| `ts` | ISO 8601 | When the call completed |
| `agent` | string | Agent type name |
| `tool` | string | Tool called |
| `args` | object | Arguments (full, not summarized) |
| `result_summary` | string | First 500 chars of serialized result |
| `approved` | bool \| null | `true`/`false` for approve-sensitivity tools, `null` for auto |
| `duration_ms` | float | Wall-clock handler execution time |
| `error` | string | Error message if the call failed, empty string otherwise |

The audit log is the shared interface between the proxy and downstream systems (context reconstructor, crystallization detector). It is append-only and never modified by the proxy after writing.

---

## Relationship to Agent Types

Agent types (defined in `agent-security-design-doc.md`) reference tools by name:

```yaml
# agent_types/email-manager.yaml
name: email-manager
tools:
  - read_inbox
  - read_newsletters
  - draft_response
  - send_email_low_risk     # narrow send — pre-approved recipients, auto
  # send_email NOT granted  — broad send requires email-admin type
```

The proxy enforces this binding. The tool registry is global; agent types select from it. When `email-manager` calls `send_email`, the proxy rejects it — `send_email` is not in this agent type's tool list, regardless of whether the tool exists in the registry.

```
Agent type: email-manager
  tools: [read_inbox, read_newsletters, draft_response, send_email_low_risk]

Tool registry: [read_inbox, read_newsletters, draft_response,
                send_email, send_email_low_risk, ...]

call_tool("read_newsletters")    → allowed (in type's list, narrow)
call_tool("read_inbox")          → allowed (in type's list, broader)
call_tool("send_email_low_risk") → allowed (auto, pre-approved recipients)
call_tool("send_email")          → rejected (not in type's list — too broad)
call_tool("delete_email")        → rejected (not in registry at all)
```

Different agent types get different levels of the hierarchy:

```yaml
# morning-digest: narrowest possible, zero approval friction
tools: [read_newsletters]

# email-manager: medium breadth, low-risk sends are automatic
tools: [read_inbox, read_newsletters, draft_response, send_email_low_risk]

# email-admin: broad access, sends require human approval
tools: [read_inbox, draft_response, send_email]
```

---

## Examples: Broad vs. Narrow Tools

These examples show the intent hierarchy in action. Each pair shares the same underlying service but differs in scope, return data, and sensitivity.

### Narrow read tool (auto, restricted scope)

`tools/read_newsletters.py`:
```python
"""
name: read_newsletters
description: |
  Read newsletter emails only. Filters to known newsletter senders,
  returns summaries (not full bodies). Use this for morning digests
  and newsletter triage.
  For all inbox mail, use read_inbox instead.
sensitivity: auto
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

### Broad read tool (auto, wider scope)

`tools/read_inbox.py`:
```python
"""
name: read_inbox
description: |
  Read recent emails from inbox. Returns subject, sender, date, and
  snippet for each email. Does NOT return full bodies.
  For newsletters only, prefer read_newsletters (narrower, pre-filtered).
  For full email bodies, use read_email_body.
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

### Narrow send tool (auto, pre-approved recipients)

`tools/send_email_low_risk.py`:
```python
"""
name: send_email_low_risk
description: |
  Send an email to a pre-approved recipient. Executes without human
  approval. The recipient must be in the approved list (e.g., unsubscribe
  addresses, automated systems, known contacts marked as low-risk).
  For sending to any recipient, use send_email (requires approval).
sensitivity: auto
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
            f"Use send_email instead (requires human approval)."
        )
    client = connect(credentials["gmail_oauth"])
    msg_id = client.send(
        to=arguments["to"],
        subject=arguments["subject"],
        body=arguments["body"],
    )
    return {"sent": True, "message_id": msg_id}
```

### Broad send tool (approve, any recipient)

`tools/send_email.py`:
```python
"""
name: send_email
description: |
  Send an email to any recipient. Requires human approval before sending.
  For pre-approved recipients (unsubscribe, known contacts), use
  send_email_low_risk instead (no approval needed).
sensitivity: approve
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

Notice the pattern: both send tools share `_lib.gmail` but have different scopes. The narrow tool validates against an approved list and runs with `auto` sensitivity. The broad tool sends to anyone but requires `approve`. The descriptions cross-reference each other so the agent knows when to prefer the narrow tool.

---

## Decisions (formerly open questions)

- **Index staleness**: INDEX.md is regenerated at proxy startup and via `harness index`. A tool-creation skill will guide the architect agent to call `harness index` at the end of creating a new set of tools, keeping the workflow explicit.

- **Live reload**: SIGHUP. Regenerates the tool registry and INDEX.md. No file-watching.

- **Tool testing**: Tests for each tool are mandatory. `handle` is a regular Python function — testable directly with `handle(args, creds)`. A `harness test tools/read_inbox.py` CLI command calls the handler with mock arguments and validates the return schema. Tests live alongside tools (e.g., `tools/test_read_inbox.py` or a `tools/_tests/` directory — prefixed with `_` so the proxy ignores them).

- **Handler language**: Python only, deferred indefinitely. Python is great glue — any external library or service can be wrapped in a Python handler. The single-file format's value comes from the unified format; adding language variants breaks that.

- **Large results**: Pagination by default. Tool results include a banner summarizing total size; the agent receives one page and can request more by calling the tool again with a `page` or `offset` parameter. The proxy doesn't handle pagination — the tool handler does. This keeps the proxy simple and lets each tool define sensible page sizes for its data type.

---

## Sources

- `agent-security-design-doc.md` — overall harness architecture, agent types, container model, credential flow
- Claude Code skill format — inspiration for single-file definitions with YAML frontmatter
- OpenAI function calling / Claude tool use — JSON Schema parameter format, tool description conventions
- CaMeL (DeepMind, 2025) — capability-based security, tool-as-permission-boundary concept
- RBAC literature (NIST SP 800-207, Zero Trust Architecture) — role-based model this design departs from
