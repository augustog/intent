from dataclasses import dataclass, field
from pathlib import Path
import json
import logging

log = logging.getLogger(__name__)


@dataclass
class Config:
    bind: str = "127.0.0.1"
    port: int = 7400
    tools_dir: Path = field(default_factory=lambda: Path("tools"))
    secrets_file: Path = field(default_factory=lambda: Path("secrets.json"))
    audit_file: Path = field(default_factory=lambda: Path("audit.jsonl"))
    token_file: Path | None = None
    socket_path: Path | None = None
    tcp: bool = False


def validate_secrets_file(path: Path) -> None:
    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        log.warning(
            "secrets file %s has mode %o, expected 0600 — fix with: chmod 600 %s",
            path, mode, path,
        )


def read_scoped_secrets(path: Path, keys: list[str]) -> dict:
    if not keys or not path.exists():
        return {}
    with open(path) as f:
        secrets = json.load(f)
    if not isinstance(secrets, dict):
        raise ValueError(f"secrets file must contain a JSON object, got {type(secrets).__name__}")
    return {k: secrets[k] for k in keys if k in secrets}
