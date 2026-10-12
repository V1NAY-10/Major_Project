"""Entry point for headless FreeCAD:  freecadcmd backend/freecad_bridge/headless.py"""
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))

from freecad_bridge import server  # noqa: E402

server.serve_forever()
