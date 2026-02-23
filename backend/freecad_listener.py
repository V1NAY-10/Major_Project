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
                # Wrap in doCommand to show in console and handle undo
                Gui.doCommand(code)
                print(f"Executed {len(code)} bytes of code.")
            except Exception as e:
                print(f"Execution error: {e}")

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
