from dataclasses import dataclass, field
from pathlib import Path
import ast
import logging

import yaml

log = logging.getLogger(__name__)


def expand_schema(parameters: dict) -> dict:
    """Convert shorthand parameter defs into JSON Schema.

    Input format per parameter:
        type: string
        description: ...
        required: true    # optional flag

    Output: standard JSON Schema object with properties + required array.
    """
    properties = {}
    required = []
    for name, spec in parameters.items():
        prop = {k: v for k, v in spec.items() if k != "required"}
        properties[name] = prop
        if spec.get("required"):
            required.append(name)
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


@dataclass
class ToolManifest:
    name: str
    description: str
    path: Path = field(default_factory=Path)
    sensitivity: str = "low"
    credentials: list[str] = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    schema: dict = field(default_factory=dict)
    timeout: int = 30
    group: str = ""


class Registry:
    def __init__(self):
        self._tools: dict[str, ToolManifest] = {}

    def load(self, tools_dir: Path):
        tools: dict[str, ToolManifest] = {}
        if not tools_dir.is_dir():
            log.warning("tools directory %s does not exist", tools_dir)
            self._tools = tools
            return
        for path in sorted(tools_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                manifest = self._load_tool(path)
                tools[manifest.name] = manifest
                log.info("loaded tool: %s", manifest.name)
            except Exception:
                log.exception("failed to load tool %s", path)
        self._tools = tools

    def _load_tool(self, path: Path) -> ToolManifest:
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        docstring = ast.get_docstring(tree)
        if not docstring:
            raise ValueError(f"tool {path} has no docstring manifest")
        meta = yaml.safe_load(docstring)
        if not isinstance(meta, dict):
            raise ValueError(f"tool {path} docstring is not valid YAML")

        name = path.stem
        params = meta.get("parameters", {})
        schema = expand_schema(params) if params else {"type": "object", "properties": {}}

        # Verify handle() exists via AST — no code execution
        has_handle = False
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle":
                raise ValueError(
                    f"tool {path} defines async handle() — "
                    "tool handlers must be synchronous"
                )
            if isinstance(node, ast.FunctionDef) and node.name == "handle":
                has_handle = True
        if not has_handle:
            raise ValueError(f"tool {path} missing handle() function")

        return ToolManifest(
            name=name,
            description=meta.get("description", ""),
            path=path.resolve(),
            sensitivity=meta.get("sensitivity", "low"),
            credentials=meta.get("credentials", []),
            parameters=params,
            schema=schema,
            timeout=meta.get("timeout", 30),
            group=meta.get("group", ""),
        )

    def get(self, name: str) -> ToolManifest | None:
        return self._tools.get(name)

    def list(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.schema,
                "group": t.group,
            }
            for t in self._tools.values()
        ]
