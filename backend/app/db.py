"""MongoDB handle (optional).

Without MONGODB_URI, or when the database can't be reached at startup, the app
keeps sessions and messages in memory. Always access it via get_db().
"""

import os

from motor.motor_asyncio import AsyncIOMotorClient

_db = None


async def init_db():
    """Connect and ping MongoDB; fall back to in-memory storage if that fails."""
    global _db
    uri = os.getenv("MONGODB_URI")
    if not uri:
        print("Warning: MONGODB_URI not set — sessions and messages are kept in memory only")
        return
    try:
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
        await client.admin.command("ping")
        _db = client[os.getenv("MONGODB_DB_NAME", "freecad_ai")]
        print("MongoDB connected")
    except Exception as e:
        _db = None
        print(f"Warning: MongoDB unreachable ({e}).\n"
              "         Falling back to in-memory sessions/messages (lost on restart). "
              "Check MONGODB_URI in backend/.env.")


def get_db():
    return _db
