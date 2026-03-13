from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import socket
import datetime
from dotenv import load_dotenv
from openai import OpenAI
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from typing import Optional, List

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_key = os.getenv("OPENROUTER_API_KEY")

if not api_key:
    print("Warning: OPENROUTER_API_KEY not found")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=api_key,
)

# MongoDB Config
mongodb_uri = os.getenv("MONGODB_URI")
db_name = os.getenv("MONGODB_DB_NAME", "freecad_ai")

db = None
if mongodb_uri:
    client_db = AsyncIOMotorClient(mongodb_uri)
    db = client_db[db_name]
else:
    print("Warning: MONGODB_URI not found in environment variables")

def format_doc(doc):
    """Helper to convert MongoDB _id (ObjectId) to id (str)."""
    if doc is None: return None
    doc["id"] = str(doc["_id"])
    del doc["_id"]
    return doc

class PromptRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None

class SessionCreate(BaseModel):
    title: str
    user_id: str

class SessionUpdate(BaseModel):
    title: str

class SyncRequest(BaseModel):
    user_id: str
    previous_session_id: Optional[str] = None

@app.get("/")
async def read_root():
    return {"message": "FastAPI backend is running"}

# --- Chat History Endpoints ---

@app.get("/sessions")
async def get_sessions(user_id: Optional[str] = None):
    if db is None: return []
    if not user_id:
        # Stop returning all sessions if user_id is missing
        print("Warning: get_sessions called without user_id")
        return []
    try:
        query = {"user_id": user_id}
        cursor = db.sessions.find(query).sort("created_at", -1)
        sessions = await cursor.to_list(length=100)
        return [format_doc(s) for s in sessions]
    except Exception as e:
        print(f"MongoDB error (get_sessions): {e}")
        return []

@app.post("/sessions")
async def create_session(session: SessionCreate):
    if db is None: raise HTTPException(status_code=503, detail="Database not configured")
    try:
        new_session = {
            "title": session.title,
            "user_id": session.user_id,
            "created_at": datetime.datetime.now(datetime.timezone.utc)
        }
        result = await db.sessions.insert_one(new_session)
        new_session["id"] = str(result.inserted_id)
        # remove internal fields if any before returning
        if "_id" in new_session: del new_session["_id"]
        return new_session
    except Exception as e:
        print(f"MongoDB error (create_session): {e}")
        raise HTTPException(status_code=500, detail="Failed to create session.")

@app.patch("/sessions/{session_id}")
async def update_session(session_id: str, session: SessionUpdate):
    if db is None: raise HTTPException(status_code=503, detail="Database not configured")
    try:
        await db.sessions.update_one(
            {"_id": ObjectId(session_id)},
            {"$set": {"title": session.title}}
        )
        return {"id": session_id, "title": session.title}
    except Exception as e:
        print(f"MongoDB error (update_session): {e}")
        raise HTTPException(status_code=500, detail="Failed to rename session.")

@app.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, user_id: str):
    if db is None: return []
    try:
        # Verify ownership first
        session = await db.sessions.find_one({"_id": ObjectId(session_id), "user_id": user_id})
        if not session:
            print(f"Unauthorized access attempt to session {session_id} by user {user_id}")
            return []
            
        cursor = db.messages.find({"session_id": session_id}).sort("created_at", 1)
        messages = await cursor.to_list(length=1000)
        return [format_doc(m) for m in messages]
    except Exception as e:
        print(f"MongoDB error (get_messages): {e}")
        return []

@app.post("/sessions/{session_id}/sync")
async def sync_session(session_id: str, request: SyncRequest):
    """Signals the FreeCAD listener to save the current doc and switch to a new one."""
    if db is not None:
        # Verify ownership
        session = await db.sessions.find_one({"_id": ObjectId(session_id) if session_id != "new" else None, "user_id": request.user_id})
        if session_id != "new" and not session:
             raise HTTPException(status_code=403, detail="Unauthorized session sync")
             
    import json
    command = {
        "action": "sync_session",
        "current_id": session_id,
        "previous_id": request.previous_session_id
    }
    # Send as internal command prefix
    internal_cmd = f"__INTERNAL_CMD__{json.dumps(command)}"
    
    print(f"--- Syncing session: {session_id} (Prev: {request.previous_session_id}) ---")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(("127.0.0.1", 6666))
            s.sendall(internal_cmd.encode('utf-8'))
            data = s.recv(1024)
            if data == b"OK":
                return {"status": "success"}
    except Exception as e:
        print(f"Sync error: {e}")
        # Don't fail the whole request, maybe the listener isn't running
        return {"status": "error", "message": str(e)}

# --- Generation Endpoint ---

@app.post("/generate")
async def generate_freecad_code(request: PromptRequest):
    if not api_key or api_key == "your_openrouter_api_key_here":
        raise HTTPException(status_code=500, detail="OpenRouter API key not configured")

    try:
        system_instruction = (
            "You are a FreeCAD Python expert. Generate ONLY a working Python script for FreeCAD. "
            "Follow these rules strictly:\n"
            "1. Use 'import Part', 'import FreeCAD as App', and 'import PartDesign'.\n"
            "2. For simple shapes, use 'Part.makeBox', 'Part.makeCylinder', etc.\n"
            "3. For complex shapes like involute gears, use the 'PartDesign' or 'Part' module API correctly.\n"
            "4. A document is pre-initialized. Use: 'doc = App.ActiveDocument'. NEVER use 'App.newDocument()'.\n"
            "5. Add objects using 'doc.addObject' and always call 'doc.recompute()'.\n"
            "6. NO markdown, NO explanations, NO comments outside the code blocks. ONLY Python code."
        )

        response = client.chat.completions.create(
            model="google/gemini-2.0-flash-001",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"User prompt: {request.prompt}"}
            ]
        )
        
        generated_code = response.choices[0].message.content

        # Basic sanitization: Extract only the content within markdown code blocks if present
        if "```" in generated_code:
            import re
            match = re.search(r"```(?:python)?\s*\n?(.*?)\n?```", generated_code, re.DOTALL)
            if match:
                generated_code = match.group(1).strip()
            else:
                # Fallback: Strip lines starting with backticks
                lines = generated_code.splitlines()
                generated_code = "\n".join([l for l in lines if not l.strip().startswith("```")]).strip()

        # Save to MongoDB if session_id is provided
        if db is not None and request.session_id:
            try:
                now = datetime.datetime.now(datetime.timezone.utc)
                await db.messages.insert_many([
                    {
                        "session_id": request.session_id, 
                        "role": "user", 
                        "content": request.prompt,
                        "created_at": now
                    },
                    {
                        "session_id": request.session_id, 
                        "role": "assistant", 
                        "content": generated_code,
                        "created_at": now + datetime.timedelta(seconds=1)
                    }
                ])
            except Exception as e:
                print(f"MongoDB save error: {e}")
                # We don't raise an exception here because we want to return the code anyway

        return {"code": generated_code}
    except Exception as e:
        print(f"Generation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/run-in-freecad")
async def run_in_freecad(request: PromptRequest):
    # The request body uses 'prompt' field to pass the code string
    code = request.prompt
    print(f"--- Attempting to send code to FreeCAD ({len(code)} bytes) ---")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2) # 2 second timeout
            s.connect(("127.0.0.1", 6666))
            s.sendall(code.encode('utf-8'))
            data = s.recv(1024)
            if data == b"OK":
                return {"status": "success", "message": "Code sent to FreeCAD successfully"}
            else:
                return {"status": "error", "message": "Unexpected response from FreeCAD"}
    except ConnectionRefusedError:
        raise HTTPException(status_code=503, detail="Could not connect to FreeCAD. Ensure the listener macro is running.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    # Enable reload for development to ensure changes are applied
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
