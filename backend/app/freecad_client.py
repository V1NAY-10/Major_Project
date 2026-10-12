"""
Client for the FreeCAD JSON-RPC bridge (backend/freecad_bridge/server.py).

If FreeCAD isn't reachable and FREECAD_AUTOSTART is "gui" (default) or "headless",
the client launches FreeCAD with the bridge and waits for it to come up.
"""

import glob
import itertools
import json
import os
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from starlette.concurrency import run_in_threadpool

HOST = os.getenv("FREECAD_HOST", "127.0.0.1")
PORT = int(os.getenv("FREECAD_PORT", "6666"))
AUTOSTART = os.getenv("FREECAD_AUTOSTART", "gui").lower()  # gui | headless | off
BACKEND_DIR = Path(__file__).resolve().parent.parent

_ids = itertools.count(1)
_start_lock = threading.Lock()


class FreeCADUnavailable(Exception):
    pass


class FreeCADError(Exception):
    def __init__(self, message, traceback=None):
        super().__init__(message)
        self.traceback = traceback


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise FreeCADUnavailable("FreeCAD closed the connection")
        buf.extend(chunk)
    return bytes(buf)


def call_sync(method, timeout=300, **params):
    try:
        sock = socket.create_connection((HOST, PORT), timeout=5)
    except OSError as e:
        raise FreeCADUnavailable(f"FreeCAD bridge not reachable on {HOST}:{PORT} ({e})")
    with sock:
        sock.settimeout(timeout)
        data = json.dumps({"id": next(_ids), "method": method, "params": params}).encode("utf-8")
        sock.sendall(struct.pack(">I", len(data)) + data)
        try:
            (length,) = struct.unpack(">I", _recv_exact(sock, 4))
            resp = json.loads(_recv_exact(sock, length).decode("utf-8"))
        except socket.timeout:
            raise FreeCADError(f"FreeCAD did not answer '{method}' within {timeout}s")
    if not resp.get("ok"):
        raise FreeCADError(resp.get("error", "unknown FreeCAD error"), resp.get("traceback"))
    return resp.get("result")


def _find_exe(env_var, names):
    env = os.getenv(env_var)
    if env and os.path.exists(env):
        return env
    roots = [os.getenv("ProgramFiles", r"C:\Program Files"), os.getenv("LOCALAPPDATA", "")]
    for root in roots:
        for d in sorted(glob.glob(os.path.join(root, "FreeCAD*")), reverse=True):
            for n in names:
                p = os.path.join(d, "bin", n)
                if os.path.exists(p):
                    return p
    for n in names:  # Linux/macOS: rely on PATH
        for d in os.getenv("PATH", "").split(os.pathsep):
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return None


def _launch():
    if AUTOSTART == "headless":
        exe = _find_exe("FREECAD_CMD_EXE", ["freecadcmd.exe", "FreeCADCmd", "freecadcmd"])
        script = BACKEND_DIR / "freecad_bridge" / "headless.py"
    else:
        exe = _find_exe("FREECAD_GUI_EXE", ["freecad.exe", "FreeCAD", "freecad"])
        script = BACKEND_DIR / "freecad_listener.py"
    if not exe:
        raise FreeCADUnavailable("FreeCAD is not running and its executable was not found. "
                                 "Set FREECAD_GUI_EXE / FREECAD_CMD_EXE or start FreeCAD and run "
                                 "backend/freecad_listener.py.")
    print(f"[FreeCAD] launching {exe} {script}")
    subprocess.Popen([exe, str(script)], cwd=str(BACKEND_DIR))


def ensure_running(wait=90):
    try:
        return call_sync("ping", timeout=10)
    except FreeCADUnavailable:
        if AUTOSTART == "off":
            raise FreeCADUnavailable("FreeCAD is not running. Open FreeCAD and run backend/freecad_listener.py.")
    with _start_lock:
        try:
            return call_sync("ping", timeout=10)
        except FreeCADUnavailable:
            pass
        _launch()
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                return call_sync("ping", timeout=10)
            except FreeCADUnavailable:
                continue
    raise FreeCADUnavailable("FreeCAD was launched but the bridge did not come up in time.")


async def call(method, timeout=300, **params):
    await run_in_threadpool(ensure_running)
    return await run_in_threadpool(lambda: call_sync(method, timeout=timeout, **params))


async def status():
    try:
        return {"connected": True, **(await run_in_threadpool(lambda: call_sync("ping", timeout=5)))}
    except Exception as e:
        return {"connected": False, "error": str(e)}
