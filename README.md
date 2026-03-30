# Intent

A standalone tool server for agent harnesses. Part of a broader multi-agent system forked from [nanoclaw](https://github.com/anthropics/claw).

## Why

This is a simple solution to the problem of agent permissioning when working with RBAC tokens or services meant for human use.

Granting broad permissions to an agent is necessary to make it useful, but also hilariously insecure. Rather than granting your agent access to your entire gmail account by ~giving it your oauth token~ injecting a broad token at the boundary, you can grant endlessly narrow permissions in the form of a tool.

Intent decomposes service-level capabilities into intent-level tools. Each tool is a single Python file with a YAML manifest and a `handle()` function. The server injects credentials, validates arguments, enforces timeouts, and logs everything. The agent sees tool names and descriptions, never credentials or implementation details.

This works with a broader sub-agent model where different sub-agent types have different levels of access.

While intended to be used in tandem with an "architect" agent (short-lived, human in the loop coding agent used to set up the system), the solution is small and simple enough to be coded by hand.

## How it works

Two HTTP endpoints over a Unix domain socket. `GET /tools` returns available tools. `POST /tools/{name}/call` executes one.

Tools live in `tools/` as single Python files. Intent parses manifests at startup via `ast.parse` (no code execution), then runs each tool in its own subprocess via `multiprocessing.Process`. Credentials are read from disk per-call, scoped to declared keys, and never persistently open.

```bash
uv run python -m intent
# prints: INTENT_TOKEN=<token> INTENT_SOCK=<path>
curl --unix-socket $INTENT_SOCK -H "Authorization: Bearer $INTENT_TOKEN" http://localhost/tools
```

## Architecture

Intent is one layer in a harness that combines nanoclaw's simplicity (small codebase, container-as-freedom-zone) with strict credential isolation:

- **Container**: full bash/python/compute freedom, optional `--network none` for zero exfiltration channels.
- **Proxy** (this project): holds credentials, exposes tools over HTTP, harness-agnostic
- **Agent types**: declarative YAML configs that specify goal of a sub-agent and which tools they get.

The proxy is ~420 LOC. The design doc (`proxy-tool-system-design.md`) is the full specification.
