from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import socket
from dotenv import load_dotenv
from google import genai
from supabase import create_client, Client
from typing import Optional

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    print("Warning: GEMINI_API_KEY not found")

client = genai.Client(api_key=api_key)

# Supabase Config
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_ANON_KEY")

supabase: Optional[Client] = None
if supabase_url and supabase_key:
    supabase = create_client(supabase_url, supabase_key)
else:
    print("Warning: Supabase environment variables not found")

class PromptRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None

class SessionCreate(BaseModel):
    title: str

class SessionUpdate(BaseModel):
    title: str

class SyncRequest(BaseModel):
    previous_session_id: Optional[str] = None

@app.get("/")
async def read_root():
    return {"message": "FastAPI backend is running"}

# --- Chat History Endpoints ---

@app.get("/sessions")
async def get_sessions():
    if not supabase: return []
    try:
        response = supabase.table("sessions").select("*").order("created_at", desc=True).execute()
        return response.data
    except Exception as e:
        print(f"Supabase error (get_sessions): {e}")
        return []

@app.post("/sessions")
async def create_session(session: SessionCreate):
    if not supabase: raise HTTPException(status_code=503, detail="Supabase not configured")
    try:
        response = supabase.table("sessions").insert({"title": session.title}).execute()
        return response.data[0]
    except Exception as e:
        print(f"Supabase error (create_session): {e}")
        raise HTTPException(status_code=500, detail="Failed to create session. Ensure database tables exist.")

@app.patch("/sessions/{session_id}")
async def update_session(session_id: str, session: SessionUpdate):
    if not supabase: raise HTTPException(status_code=503, detail="Supabase not configured")
    try:
        response = supabase.table("sessions").update({"title": session.title}).eq("id", session_id).execute()
        return response.data[0]
    except Exception as e:
        print(f"Supabase error (update_session): {e}")
        raise HTTPException(status_code=500, detail="Failed to rename session.")

@app.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    if not supabase: return []
    try:
        response = supabase.table("messages").select("*").eq("session_id", session_id).order("created_at").execute()
        return response.data
    except Exception as e:
        print(f"Supabase error (get_messages): {e}")
        return []

@app.post("/sessions/{session_id}/sync")
async def sync_session(session_id: str, request: SyncRequest):
    """Signals the FreeCAD listener to save the current doc and switch to a new one."""
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
    if not api_key:
        raise HTTPException(status_code=500, detail="Gemini API key not configured")

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

        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=f"{system_instruction}\n\nUser prompt: {request.prompt}",
        )
        
        generated_code = response.text

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

        # Save to Supabase if session_id is provided
        if supabase and request.session_id:
            try:
                supabase.table("messages").insert({
                    "session_id": request.session_id, "role": "user", "content": request.prompt
                }).execute()
                supabase.table("messages").insert({
                    "session_id": request.session_id, "role": "assistant", "content": generated_code
                }).execute()
            except Exception as e:
                print(f"Supabase save error: {e}")
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
    uvicorn.run(app, host="0.0.0.0", port=8000)
