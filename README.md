# Intent

A standalone tool server for agent harnesses. Part of a broader multi-agent system built on [nanoclaw](https://github.com/anthropics/claw) architecture.

## Why

Traditional agent permissioning uses service-level roles: "this agent can access email." That's too broad. If there's no `forward_email` tool, the instruction "forward this to evil@example.com" is inert — the tool boundary is the permission boundary.

Intent decomposes service-level capabilities into intent-level tools. Each tool is a single Python file with a YAML manifest and a `handle()` function. The server injects credentials, validates arguments, enforces timeouts, and logs everything. The agent sees tool names and descriptions, never credentials or implementation details.

## How it works

Two HTTP endpoints on localhost. `GET /tools` returns available tools. `POST /tools/{name}/call` executes one.

Tools live in `tools/` as single Python files. Intent parses manifests at startup via `ast.parse` (no code execution), then runs each tool in its own subprocess via `multiprocessing.Process`. Credentials are read from disk per-call, scoped to declared keys, and never held resident in memory.

```bash
uv run python -m intent
curl -H "Authorization: Bearer $(cat token)" http://127.0.0.1:7400/tools
```

## Architecture

Intent is one layer in a harness that combines nanoclaw's simplicity (small codebase, container-as-freedom-zone) with strict credential isolation:

- **Container** (`--network none`): full bash/python/compute freedom, zero exfiltration channel
- **Proxy** (this project): holds credentials, exposes tools over HTTP, harness-agnostic
- **Agent types**: declarative YAML configs that specify which tools each agent gets

The proxy is ~420 LOC. The design doc (`proxy-tool-system-design.md`) is the full specification.
