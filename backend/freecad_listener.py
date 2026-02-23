import socket
import threading
import FreeCAD as App
import FreeCADGui as Gui
import time

# Try to import PySide for thread-safe execution via QTimer
try:
    from PySide6 import QtCore
except ImportError:
    try:
        from PySide2 import QtCore
    except ImportError:
        QtCore = None

class ExecutionManager:
    def __init__(self):
        self.queue = []
        if QtCore:
            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self.process_queue)
            self.timer.start(100) # Check every 100ms
            print("Execution manager using QTimer started.")
        else:
            print("Warning: PySide not found. Falling back to non-thread-safe execution (expect crashes).")

    def process_queue(self):
        while self.queue:
            code = self.queue.pop(0)
            try:
                # Check for internal commands
                if code.startswith("__INTERNAL_CMD__"):
                    import json, os
                    cmd_data = json.loads(code.replace("__INTERNAL_CMD__", ""))
                    if cmd_data.get("action") == "sync_session":
                        self.handle_sync_session(cmd_data)
                    continue

                # Normal FreeCAD script execution
                # Safety sanitization: remove potential markdown markers
                if "```" in code:
                    import re
                    match = re.search(r"```(?:python)?\s*\n?(.*?)\n?```", code, re.DOTALL)
                    if match:
                        code = match.group(1).strip()
                    else:
                        lines = code.splitlines()
                        code = "\n".join([l for l in lines if not l.strip().startswith("```")]).strip()

                if not App.ActiveDocument:
                    App.newDocument("Untitled")
                    Gui.updateGui()
                    
                Gui.doCommand(code)
                print(f"Executed {len(code)} bytes of code.")
            except Exception as e:
                print(f"Execution error: {e}")

    def handle_sync_session(self, data):
        import os
        prev_id = data.get("previous_id")
        curr_id = data.get("current_id")
        
        # Use absolute path relative to this script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        exports_dir = os.path.join(script_dir, "exports")
        
        if not os.path.exists(exports_dir):
            os.makedirs(exports_dir)
            
        print(f"Syncing: {prev_id} -> {curr_id}")

        # 1. Save current document as the previous session if it exists
        if prev_id and prev_id != "new":
            try:
                doc = App.ActiveDocument
                if doc:
                    save_path = os.path.join(exports_dir, f"{prev_id}.FCStd")
                    doc.saveAs(save_path)
                    print(f"Saved session {prev_id} to {save_path}")
            except Exception as e:
                print(f"Error saving previous session: {e}")

        # 2. Open or create the current session document
        is_new = (curr_id == "new")
        doc_label = "New Chat" if is_new else f"Chat: {curr_id[:8]}"
        curr_path = os.path.join(exports_dir, f"{curr_id}.FCStd")
        
        try:
            if is_new:
                # Always create a FRESH "New Chat"
                # If one already exists with this label, rename it to avoid confusion
                for d in App.listDocuments().values():
                    if d.Label == "New Chat":
                        d.Label = f"Old Chat {int(time.time())}"
                
                doc = App.newDocument()
                doc.Label = "New Chat"
                print("Created fresh project for New Chat")
            else:
                # Check if a document with this label is already open
                found_doc = None
                for d in App.listDocuments().values():
                    if d.Label == doc_label:
                        found_doc = d
                        break
                
                if found_doc:
                    App.setActiveDocument(found_doc.Name)
                    print(f"Switched to existing session: {found_doc.Label}")
                elif os.path.exists(curr_path):
                    # Open existing file
                    App.open(curr_path)
                    new_doc = App.ActiveDocument
                    new_doc.Label = doc_label
                    print(f"Opened session file: {curr_path}")
                else:
                    # Create new document for this session
                    doc = App.newDocument()
                    doc.Label = doc_label
                    print(f"Created new document for session: {doc_label}")
            
            # Force UI update
            Gui.updateGui()
        except Exception as e:
            print(f"Error switching/creating session document: {e}")

    def add_to_queue(self, code):
        self.queue.append(code)

# Global manager instance
manager = ExecutionManager()

def run_listener():
    HOST = '127.0.0.1'
    PORT = 6666
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((HOST, PORT))
        except OSError as e:
            if e.errno == 10048:
                print(f"Error: Port {PORT} is already in use. Listener may already be running.")
                return
            else:
                raise e
                
        s.listen()
        print(f"FreeCAD Listener started on {HOST}:{PORT}")
        
        while True:
            conn, addr = s.accept()
            with conn:
                data = conn.recv(65536).decode('utf-8')
                if data:
                    print(f"Received code ({len(data)} bytes). Queuing...")
                    if QtCore:
                        manager.add_to_queue(data)
                        conn.sendall(b"OK")
                    else:
                        # Risky fallback
                        exec(data)
                        conn.sendall(b"OK")

# Run in a background thread
threading.Thread(target=run_listener, daemon=True).start()
