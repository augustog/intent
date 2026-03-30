import argparse
import logging
import multiprocessing
import os
import secrets
import signal

from pathlib import Path
from starlette.applications import Starlette
from starlette.routing import Route
import uvicorn

from .auth import AuthMiddleware, generate_token
from .config import Config, validate_secrets_file
from .registry import Registry
from .audit import AuditLog
from .pool import ProcessPool
from .dispatch import list_tools, call_tool

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def _default_socket_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime_dir) / f"intent-{secrets.token_hex(8)}.sock"


def main():
    multiprocessing.set_start_method("spawn")

    p = argparse.ArgumentParser(prog="intent")
    p.add_argument("--tcp", action="store_true",
                    help="listen on TCP instead of Unix domain socket (less secure)")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7400)
    p.add_argument("--uds", default=None,
                    help="explicit Unix domain socket path (default: auto-generated)")
    p.add_argument("--tools-dir", default="tools")
    p.add_argument("--secrets", default="secrets.json")
    p.add_argument("--audit", default="audit.jsonl")
    p.add_argument("--token-file", default=None,
                    help="write token to file (default: token printed to stdout only)")
    args = p.parse_args()

    token_file = Path(args.token_file) if args.token_file else None

    if args.tcp:
        socket_path = None
        log.warning(
            "TCP mode: token file is readable by any same-user process. "
            "Prefer default UDS mode for production use."
        )
        if token_file is None:
            token_file = Path("token")
    else:
        socket_path = Path(args.uds) if args.uds else _default_socket_path()

    cfg = Config(
        bind=args.bind,
        port=args.port,
        tools_dir=Path(args.tools_dir),
        secrets_file=Path(args.secrets),
        audit_file=Path(args.audit),
        token_file=token_file,
        socket_path=socket_path,
        tcp=args.tcp,
    )

    token = generate_token(cfg.token_file)
    validate_secrets_file(cfg.secrets_file)

    registry = Registry()
    registry.load(cfg.tools_dir)

    audit = AuditLog(cfg.audit_file)
    pool = ProcessPool()

    app = Starlette(routes=[
        Route("/tools", list_tools),
        Route("/tools/{name}/call", call_tool, methods=["POST"]),
    ])
    app.state.registry = registry
    app.state.audit = audit
    app.state.pool = pool
    app.state.cfg = cfg

    # SIGHUP reloads tools — kill workers so they respawn with new code
    def _reload(signum, frame):
        log.info("SIGHUP received — reloading tools")
        pool.shutdown()
        registry.load(cfg.tools_dir)

    signal.signal(signal.SIGHUP, _reload)

    # Convert SIGTERM → SIGINT so uvicorn shuts down gracefully
    # (uvicorn handles SIGINT but not SIGTERM for socket cleanup)
    signal.signal(signal.SIGTERM, lambda s, f: os.kill(os.getpid(), signal.SIGINT))

    app = AuthMiddleware(app, token)

    # Emit machine-readable startup line for harness capture
    if cfg.socket_path:
        print(f"INTENT_TOKEN={token} INTENT_SOCK={cfg.socket_path}", flush=True)
    else:
        print(f"INTENT_TOKEN={token} INTENT_ADDR={cfg.bind}:{cfg.port}", flush=True)

    try:
        if cfg.socket_path:
            uvicorn.run(app, uds=str(cfg.socket_path), log_level="warning")
        else:
            uvicorn.run(app, host=cfg.bind, port=cfg.port, log_level="warning")
    finally:
        pool.shutdown()
        if cfg.socket_path and cfg.socket_path.exists():
            cfg.socket_path.unlink()


main()
