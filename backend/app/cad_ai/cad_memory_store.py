"""
CAD Memory Store — In-memory per-session CAD data storage.

Stores only the latest CAD context per session. Overwrites on new upload.
No database dependency — purely in-process dict.
"""

import threading
from typing import Optional, Dict, Any


class CADMemoryStore:
    """Thread-safe in-memory store for parsed CAD data, keyed by session_id."""

    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def store(self, session_id: str, file_id: str, parsed_data: dict, stl_data: bytes = None, raw_cad_data: bytes = None) -> None:
        """
        Store (or overwrite) CAD context for a session.
        Only the latest CAD upload is retained per session.
        """
        with self._lock:
            self._store[session_id] = {
                "file_id": file_id,
                "parsed_data": parsed_data,
                "stl_data": stl_data,
                "raw_cad_data": raw_cad_data,
            }

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the stored CAD context for a session, or None."""
        with self._lock:
            return self._store.get(session_id)

    def get_by_file_id(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Look up CAD context by file_id across all sessions."""
        with self._lock:
            for sid, entry in self._store.items():
                if entry["file_id"] == file_id:
                    return {**entry, "session_id": sid}
            return None

    def has_context(self, session_id: str) -> bool:
        """Check whether a session has stored CAD context."""
        with self._lock:
            return session_id in self._store

    def remove(self, session_id: str) -> bool:
        """Remove CAD context for a session. Returns True if existed."""
        with self._lock:
            return self._store.pop(session_id, None) is not None

    def clear(self) -> None:
        """Wipe all stored CAD contexts."""
        with self._lock:
            self._store.clear()


# Singleton — imported by routes and main
cad_store = CADMemoryStore()
