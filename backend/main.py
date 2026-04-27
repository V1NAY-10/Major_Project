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
import json
import re

# CAD AI module imports
from app.cad_ai.routes import router as cad_router
from app.cad_ai.cad_context_manager import session_has_cad, get_cad_context
from app.cad_ai.prompt_builder import build_cad_prompt

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://192.168.29.245:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount CAD AI routes (prefix=/cad)
app.include_router(cad_router)


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
fs = None
if mongodb_uri:
    from motor.motor_asyncio import AsyncIOMotorGridFSBucket
    client_db = AsyncIOMotorClient(mongodb_uri)
    db = client_db[db_name]
    fs = AsyncIOMotorGridFSBucket(db)
else:
    print("Warning: MONGODB_URI not found in environment variables")

def format_doc(doc):
    """Helper to convert MongoDB _id (ObjectId) to id (str)."""
    if doc is None: return None
    doc["id"] = str(doc["_id"])
    del doc["_id"]
    for key, value in list(doc.items()):
        if isinstance(value, ObjectId):
            doc[key] = str(value)
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

@app.get("/sessions/{session_id}")
async def get_session(session_id: str, user_id: str):
    if db is None: raise HTTPException(status_code=503, detail="Database not configured")
    try:
        session = await db.sessions.find_one({"_id": ObjectId(session_id), "user_id": user_id})
        if not session:
             raise HTTPException(status_code=404, detail="Session not found")
        return format_doc(session)
    except Exception as e:
        print(f"MongoDB error (get_session): {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
async def generate_response(request: PromptRequest):
    if not api_key or api_key == "your_openrouter_api_key_here":
        raise HTTPException(status_code=500, detail="OpenRouter API key not configured")

    try:
        # Save user message to DB
        if db is not None and request.session_id:
            now = datetime.datetime.now(datetime.timezone.utc)
            await db.messages.insert_one({
                "session_id": request.session_id,
                "role": "user",
                "content": request.prompt,
                "created_at": now
            })

        # Fetch chat history (limit to last 20 messages for context)
        chat_history = []
        if db is not None and request.session_id:
            # We sort by created_at to get chronological order
            cursor = db.messages.find({"session_id": request.session_id}).sort("created_at", 1)
            docs = await cursor.to_list(length=20)
            for doc in docs:
                if doc.get("role") in ["user", "assistant"] and doc.get("content"):
                    chat_history.append({"role": doc["role"], "content": doc["content"]})
        else:
            chat_history = [{"role": "user", "content": request.prompt}]

        cad_ctx = await get_cad_context(request.session_id) if request.session_id else None
        has_cad = cad_ctx is not None
        parsed_data = None
        
        # ── CAD-aware branch ────────────────────────────────────────────
        if has_cad:
            parsed_data = cad_ctx.get("parsed_data")

            # First, get a conversational response (text + code)
            system_instruction = (
                "You are an expert CAD AI assistant. You can both answer engineering questions and modify 3D models.\n\n"
                "CONTEXT:\n"
                f"Parsed CAD Data: {json.dumps(parsed_data, indent=2) if parsed_data else 'No active CAD data found.'}\n\n"
                "USER CAPABILITIES:\n"
                "1. If the user asks a question about the model, answer concisely in text.\n"
                "2. If the user asks to MODIFY the model, generate a FreeCAD Python script.\n"
                "3. ALWAYS wrap Python code in ```python ... ``` blocks.\n\n"
                "FREECAD SCRIPT RULES:\n"
                "- Use 'import Part', 'import FreeCAD as App', 'import PartDesign'.\n"
                "- Use 'doc = App.ActiveDocument'.\n"
                "- Access existing objects by name from the 'Parsed CAD Data' provided.\n"
                "- Always call 'doc.recompute()' at the end."
            )
            
            messages = [{"role": "system", "content": system_instruction}] + chat_history

            response = client.chat.completions.create(
                model="google/gemini-2.0-flash-001",
                messages=messages,
            )

            full_response = response.choices[0].message.content
            generated_code = ""
            if "```" in full_response:
                match = re.search(r"```(?:python)?\s*\n?(.*?)\n?```", full_response, re.DOTALL)
                if match: generated_code = match.group(1).strip()

            # If there's code, or the prompt looks like a modification, run the "Intent Interpreter"
            intents_data = None
            if generated_code or any(word in request.prompt.lower() for word in ["increase", "decrease", "change", "modify", "remove", "add"]):
                try:
                    from app.cad_ai.routes import interpret_cad_intent, InterpretRequest
                    intent_res = await interpret_cad_intent(InterpretRequest(prompt=request.prompt, parsed_data=parsed_data or {}))
                    intents_data = intent_res
                except Exception as e:
                    print(f"Intent interpretation failed in unified flow: {e}")

            # Save and return
            if db is not None and request.session_id:
                now = datetime.datetime.now(datetime.timezone.utc)
                await db.messages.insert_one({
                    "session_id": request.session_id,
                    "role": "assistant",
                    "content": full_response,
                    "code": generated_code,
                    "intents": intents_data.get("intents") if intents_data else None,
                    "preview": intents_data.get("preview") if intents_data else None,
                    "created_at": now
                })

            return {
                "content": full_response,
                "code": generated_code,
                "intents": intents_data.get("intents") if intents_data else None,
                "preview": intents_data.get("preview") if intents_data else None,
                "cad_context_used": True
            }

        else:
            # Original non-CAD flow
            system_instruction = (
                "You are a FreeCAD Python expert. Generate helpful responses. "
                "If asked to create a model, provide ONLY the Python script in a ```python ... ``` block. "
                "Otherwise, answer the user's question normally."
            )
            messages = [{"role": "system", "content": system_instruction}] + chat_history
            response = client.chat.completions.create(
                model="google/gemini-2.0-flash-001",
                messages=messages,
            )
            full_response = response.choices[0].message.content
            generated_code = ""
            if "```" in full_response:
                match = re.search(r"```(?:python)?\s*\n?(.*?)\n?```", full_response, re.DOTALL)
                if match: generated_code = match.group(1).strip()
            
            # Save to DB even for non-CAD
            if db is not None and request.session_id:
                 now = datetime.datetime.now(datetime.timezone.utc)
                 await db.messages.insert_one({
                    "session_id": request.session_id,
                    "role": "assistant",
                    "content": full_response,
                    "code": generated_code,
                    "created_at": now
                })

            return {
                "content": full_response,
                "code": generated_code,
                "cad_context_used": False
            }
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
