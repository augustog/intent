import asyncio
import logging
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

import jsonschema

from .registry import Registry
from .audit import AuditLog
from .pool import ProcessPool
from .config import read_scoped_secrets

log = logging.getLogger(__name__)


async def list_tools(request: Request) -> JSONResponse:
    registry: Registry = request.app.state.registry
    return JSONResponse({"tools": registry.list()})


async def call_tool(request: Request) -> JSONResponse:
    registry: Registry = request.app.state.registry
    audit: AuditLog = request.app.state.audit
    pool: ProcessPool = request.app.state.pool
    cfg = request.app.state.cfg

    name = request.path_params["name"]
    tool = registry.get(name)
    if tool is None:
        return JSONResponse({"error": f"tool not found: {name}"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    arguments = body.get("arguments", {})

    # Validate arguments against schema
    try:
        jsonschema.validate(arguments, tool.schema)
    except jsonschema.ValidationError as e:
        return JSONResponse({"error": f"validation: {e.message}"}, status_code=400)

    # Read credentials from disk per-call, scoped to declared keys
    credentials = read_scoped_secrets(cfg.secrets_file, tool.credentials)

    loop = asyncio.get_running_loop()
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None, pool.call, name, str(tool.path), arguments, credentials,
            ),
            timeout=tool.timeout,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        summary = str(result)[:200] if result is not None else ""
        audit.log(tool=name, args=arguments, result_summary=summary,
                  duration_ms=duration_ms, sensitivity=tool.sensitivity)
        return JSONResponse({"result": result})
    except asyncio.TimeoutError:
        duration_ms = int((time.monotonic() - t0) * 1000)
        pool.kill_worker(name)
        audit.log(tool=name, args=arguments, error="timeout",
                  duration_ms=duration_ms, sensitivity=tool.sensitivity)
        return JSONResponse({"error": f"tool timed out after {tool.timeout}s"}, status_code=504)
    except Exception as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        error_msg = f"{type(e).__name__}: {e}"
        audit.log(tool=name, args=arguments, error=error_msg,
                  duration_ms=duration_ms, sensitivity=tool.sensitivity)
        log.exception("tool %s raised", name)
        return JSONResponse({"error": error_msg}, status_code=500)
