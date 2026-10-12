"""
AetherCAD FreeCAD listener — run this inside the FreeCAD GUI.

    Macro ▸ Macros… ▸ select this file ▸ Execute
or in FreeCAD's Python console:
    exec(open(r"C:/VINAY_MAIN/Major_Project/backend/freecad_listener.py").read())

It starts the JSON-RPC bridge (freecad_bridge/server.py) on 127.0.0.1:6666.
Running it again while the bridge is up hot-reloads freecad_bridge/engine.py.
"""
import os
import sys

try:
    _BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:  # exec(open(...).read()) from the console has no __file__
    _BACKEND_DIR = os.environ.get("AETHERCAD_BACKEND", r"C:\VINAY_MAIN\Major_Project\backend")

if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from freecad_bridge import server  # noqa: E402

server.start_gui()
