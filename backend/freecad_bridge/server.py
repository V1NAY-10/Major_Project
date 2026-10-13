"""
JSON-RPC bridge server — runs inside FreeCAD.

Wire format (both directions): 4-byte big-endian length + UTF-8 JSON.
  request : {"id": ..., "method": "apply", "params": {...}}
  response: {"id": ..., "ok": true, "result": {...}}  |  {"id": ..., "ok": false, "error": "...", "traceback": "..."}

GUI mode : a socket thread receives requests and hands them to the Qt main
           thread through a queue polled by a QTimer (FreeCAD is not thread-safe).
Headless : serve_forever() handles requests sequentially on the calling thread.
"""

import importlib
import json
import os
import queue
import socket
import struct
import threading
import traceback

HOST = "127.0.0.1"
PORT = int(os.environ.get("FREECAD_PORT", "6666"))  # must match the backend's FREECAD_PORT
REQUEST_TIMEOUT = 600  # seconds a socket thread waits for the main thread

_state = {"running": False, "timer": None, "queue": None}


def _recv_exact(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise ConnectionError("connection closed mid-message")
        buf.extend(chunk)
    return bytes(buf)


def read_msg(conn):
    (length,) = struct.unpack(">I", _recv_exact(conn, 4))
    return json.loads(_recv_exact(conn, length).decode("utf-8"))


def write_msg(conn, obj):
    data = json.dumps(obj, default=str).encode("utf-8")
    conn.sendall(struct.pack(">I", len(data)) + data)


def _reload():
    """Reload analysis first, then engine (which imports it)."""
    from freecad_bridge import analysis, engine
    importlib.reload(analysis)
    importlib.reload(engine)


def _engine():
    from freecad_bridge import engine
    return engine


def handle(request):
    method = request.get("method")
    if method == "reload_engine":
        _reload()
        resp = {"ok": True, "result": {"reloaded": True}}
    else:
        resp = _engine().dispatch(method, request.get("params") or {})
    resp["id"] = request.get("id")
    return resp


def _bind():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, PORT))
    s.listen()
    return s


# ── Headless ────────────────────────────────────────────────────────────────

def serve_forever():
    srv = _bind()
    _state["running"] = True
    print(f"[AetherCAD] headless FreeCAD bridge listening on {HOST}:{PORT}", flush=True)
    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                req = read_msg(conn)
            except Exception as e:
                print(f"[AetherCAD] bad request: {e}", flush=True)
                continue
            try:
                resp = handle(req)
            except Exception as e:
                resp = {"id": req.get("id"), "ok": False, "error": str(e), "traceback": traceback.format_exc()}
            try:
                write_msg(conn, resp)
            except Exception as e:
                print(f"[AetherCAD] failed to send response: {e}", flush=True)


# ── GUI ─────────────────────────────────────────────────────────────────────

def _process_queue():
    q = _state["queue"]
    while True:
        try:
            req, done, holder = q.get_nowait()
        except queue.Empty:
            return
        try:
            holder["resp"] = handle(req)
        except Exception as e:
            holder["resp"] = {"id": req.get("id"), "ok": False, "error": str(e),
                              "traceback": traceback.format_exc()}
        finally:
            done.set()


def _serve_gui_socket(srv):
    def client(conn):
        with conn:
            try:
                req = read_msg(conn)
            except Exception as e:
                print(f"[AetherCAD] bad request: {e}")
                return
            done, holder = threading.Event(), {}
            _state["queue"].put((req, done, holder))
            if not done.wait(REQUEST_TIMEOUT):
                holder["resp"] = {"id": req.get("id"), "ok": False, "error": "FreeCAD main thread timed out"}
            try:
                write_msg(conn, holder["resp"])
            except Exception as e:
                print(f"[AetherCAD] failed to send response: {e}")

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=client, args=(conn,), daemon=True).start()


def start_gui():
    """Start the bridge inside the FreeCAD GUI (call from the main thread)."""
    if _state["running"]:
        _reload()
        print("[AetherCAD] bridge already running — engine reloaded")
        return
    try:
        from PySide import QtCore  # type: ignore  (FreeCAD's PySide shim)
    except ImportError:
        try:
            from PySide6 import QtCore  # type: ignore
        except ImportError:
            from PySide2 import QtCore  # type: ignore
    try:
        srv = _bind()
    except OSError as e:
        print(f"[AetherCAD] cannot bind {HOST}:{PORT} ({e}). Is another listener (or freecadcmd) running?")
        return
    _state["queue"] = queue.Queue()
    timer = QtCore.QTimer()
    timer.timeout.connect(_process_queue)
    timer.start(50)
    _state["timer"] = timer  # keep a reference so it isn't garbage-collected
    threading.Thread(target=_serve_gui_socket, args=(srv,), daemon=True).start()
    _state["running"] = True
    print(f"[AetherCAD] FreeCAD bridge listening on {HOST}:{PORT} (GUI mode)")
