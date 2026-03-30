from pathlib import Path
import json
import logging
import os
import time

_log = logging.getLogger(__name__)


class AuditLog:
    def __init__(self, path: Path):
        self._fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    def log(self, *, tool: str, args: dict, result_summary: str = "",
            error: str = "", duration_ms: int = 0, sensitivity: str = "low"):
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool,
            "args": args,
            "result_summary": result_summary,
            "error": error,
            "duration_ms": duration_ms,
            "sensitivity": sensitivity,
        }
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        os.write(self._fd, line.encode())
        if sensitivity == "high":
            _log.warning("high-sensitivity tool call: %s", tool)
        else:
            _log.debug("tool call: %s", tool)
