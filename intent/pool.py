from multiprocessing import Process, Pipe
from pathlib import Path
import importlib.util
import logging
import sys
import threading
import traceback

log = logging.getLogger(__name__)


def _tool_worker(tool_path: str, conn):
    """Run in a spawned subprocess. Loads the tool module once, then
    loops receiving (arguments, credentials) and sending back results."""
    path = Path(tool_path)
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    spec = importlib.util.spec_from_file_location(f"tools.{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handle = module.handle

    try:
        while True:
            msg = conn.recv()
            if msg is None:
                break
            arguments, credentials = msg
            try:
                result = handle(arguments, credentials)
                conn.send(("ok", result))
            except Exception:
                conn.send(("error", traceback.format_exc()))
    except EOFError:
        pass
    finally:
        conn.close()


class ToolProcess:
    def __init__(self, tool_path: str):
        self._tool_path = tool_path
        self._process: Process | None = None
        self._conn = None
        self._lock = threading.Lock()

    def _ensure_running(self):
        if self._process is not None and self._process.is_alive():
            return
        if self._process is not None:
            self._process.join(timeout=1)
        parent_conn, child_conn = Pipe()
        p = Process(target=_tool_worker, args=(self._tool_path, child_conn), daemon=True)
        p.start()
        child_conn.close()
        self._conn = parent_conn
        self._process = p

    def call(self, arguments: dict, credentials: dict) -> dict:
        with self._lock:
            self._ensure_running()
            self._conn.send((arguments, credentials))
            try:
                status, payload = self._conn.recv()
            except EOFError:
                self._cleanup()
                raise RuntimeError("tool worker crashed")
        if status == "error":
            raise RuntimeError(payload)
        return payload

    def kill(self):
        with self._lock:
            self._cleanup()

    def _cleanup(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._process is not None:
            self._process.terminate()
            self._process.join(timeout=2)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=1)
            self._process = None


class ProcessPool:
    def __init__(self):
        self._workers: dict[str, ToolProcess] = {}
        self._lock = threading.Lock()

    def call(self, name: str, tool_path: str, arguments: dict, credentials: dict) -> dict:
        with self._lock:
            if name not in self._workers:
                self._workers[name] = ToolProcess(tool_path)
            worker = self._workers[name]
        return worker.call(arguments, credentials)

    def kill_worker(self, name: str):
        with self._lock:
            worker = self._workers.pop(name, None)
        if worker:
            worker.kill()

    def shutdown(self):
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.kill()
