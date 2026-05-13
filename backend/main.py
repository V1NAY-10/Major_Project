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


api_key = os.getenv("GROQ_API_KEY")

if not api_key:
    print("Warning: GROQ_API_KEY not found")

client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=api_key or "missing_key",
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

# Fields that store raw binary blobs — never include in JSON API responses
_BINARY_FIELDS = frozenset({
    "cad_raw_data",
    "cad_stl_data",
})

def format_doc(doc):
    """Convert MongoDB document to a JSON-serializable dict.

    - Converts ObjectId → str
    - Strips raw binary blob fields (cad_raw_data, cad_stl_data) that would
      cause UnicodeDecodeError when FastAPI tries to JSON-encode them
    - Converts any remaining bytes values to a '<binary N bytes>' placeholder
    """
    if doc is None:
        return None
    doc["id"] = str(doc["_id"])
    del doc["_id"]
    for key in list(doc.keys()):
        value = doc[key]
        # Always drop heavy binary blobs — frontend fetches via /cad/model/
        if key in _BINARY_FIELDS:
            del doc[key]
            continue
        if isinstance(value, ObjectId):
            doc[key] = str(value)
        elif isinstance(value, bytes):
            # Safety net for any other byte fields
            doc[key] = f"<binary {len(value)} bytes>"
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
    if not api_key or api_key == "your_groq_api_key_here":
        raise HTTPException(status_code=500, detail="Groq API key not configured")

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

            parsed_format = parsed_data.get("format", "STEP") if parsed_data else "STEP"
            is_fcstd = parsed_format == "FCStd"

            # Get bounding box info for STEP files to inject into prompt
            bbox = {}
            if parsed_data:
                bbox = parsed_data.get("summary", {}).get("bounding_box", {})

            if is_fcstd:
                feature_names = [f.get("name","?") for f in (parsed_data or {}).get("features",[])[:30]]
                format_rules = (
                    "FILE TYPE: Native FreeCAD (.FCStd) -- PARAMETRIC\n"
                    f"Available named objects: {', '.join(feature_names)}\n"
                    "HOW TO MODIFY:\n"
                    "  obj = doc.getObject('ExactObjectName')  # exact names above only\n"
                    "  obj.Length = 50.0\n"
                    "  obj.Radius = 10.0\n"
                    "  doc.recompute()\n"
                    "  doc.save()\n"
                )
            else:
                bbox_info = (
                    f"X: {bbox.get('xmin',0):.2f} to {bbox.get('xmax',0):.2f} (length={bbox.get('length',0):.2f})\n"
                    f"Y: {bbox.get('ymin',0):.2f} to {bbox.get('ymax',0):.2f} (width={bbox.get('width',0):.2f})\n"
                    f"Z: {bbox.get('zmin',0):.2f} to {bbox.get('zmax',0):.2f} (height={bbox.get('height',0):.2f})"
                ) if bbox else "Bounding box not available"

                format_rules = (
                    "FILE TYPE: STEP / Non-parametric B-rep\n"
                    "CRITICAL RULES FOR STEP FILES:\n"
                    "1. 'plane_1', 'plane_2', 'hole_1' etc. are our parser labels ONLY -- "
                    "they are NOT FreeCAD object names. doc.getObject('plane_1') = None.\n"
                    "2. STEP-imported objects live in doc.Objects as Part::Feature shapes.\n"
                    "3. To resize/modify, use Shape.transformGeometry(matrix).\n\n"
                    "CORRECT TEMPLATE FOR STEP MODIFICATIONS:\n"
                    "```python\n"
                    "import Part, FreeCAD as App\n"
                    "doc = App.ActiveDocument\n"
                    "# Get the main solid (STEP import creates one or more Part::Feature objects)\n"
                    "shape_obj = next((o for o in doc.Objects if hasattr(o,'Shape') and o.Shape.Solids), None)\n"
                    "if shape_obj:\n"
                    "    shape = shape_obj.Shape\n"
                    "    bbox = shape.BoundBox\n"
                    "    # Scale: set A11=sx, A22=sy, A33=sz (identity = 1.0)\n"
                    "    m = App.Matrix()\n"
                    "    m.A11 = NEW_X / bbox.XLength   # X scale factor\n"
                    "    m.A22 = NEW_Y / bbox.YLength   # Y scale factor\n"
                    "    m.A33 = 1.0                     # Z unchanged\n"
                    "    shape_obj.Shape = shape.transformGeometry(m)\n"
                    "\n"
                    "    # Example 2: Adding a new shape (e.g. cylinder) at [X, Y, Z]\n"
                    "    # new_shape = Part.makeCylinder(radius, height)\n"
                    "    # new_shape.Placement.Base = App.Vector(X, Y, Z)\n"
                    "    # shape_obj.Shape = shape.fuse(new_shape)\n"
                    "    doc.recompute()\n"
                    "```\n"
                    "FORBIDDEN API CALLS (crash FreeCAD):\n"
                    "  x App.Matrix().translate()  -- no such method. Use m.A14=dx; m.A24=dy; m.A34=dz\n"
                    "  x doc.getObject('plane_N')  -- always None for STEP\n"
                    "  x obj.Placement.Matrix = .. -- use transformGeometry instead\n\n"
                    f"ACTUAL MODEL DIMENSIONS:\n{bbox_info}\n"
                )

            system_instruction = (
                "You are an expert CAD AI assistant and FreeCAD Python developer.\n\n"
                "COORDINATE SYSTEM:\n"
                "- X=LEFT/RIGHT  Y=FRONT/BACK  Z=BOTTOM/TOP\n"
                "- Each component has a 'center' [X, Y, Z] coordinate in mm.\n\n"
                f"{format_rules}\n"
                "PARSED CAD CONTEXT (for understanding the model):\n"
                f"{json.dumps(parsed_data, indent=2) if parsed_data else 'No CAD data.'}\n\n"
                "RESPONSE RULES:\n"
                "1. Questions: answer with XYZ references.\n"
                "2. Modifications: write FreeCAD Python following the FILE TYPE rules above exactly.\n"
                "3. Always wrap code in ```python ... ``` blocks.\n"
                "4. After code, state what changed and where (XYZ).\n"
            )


            
            # Cap history to last 10 exchanges to avoid token overflow
            messages = [{"role": "system", "content": system_instruction}] + chat_history[-10:]

            try:
                response = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=messages,
                )
            except Exception as llm_err:
                err_str = str(llm_err).lower()
                if "rate_limit" in err_str or "rate limit" in err_str:
                    raise HTTPException(
                        status_code=429,
                        detail="Groq rate limit reached. You are on the free tier — wait 1 minute and try again, or check https://console.groq.com/settings/billing to upgrade."
                    )
                raise HTTPException(status_code=500, detail=f"LLM error: {llm_err}")



            full_response = response.choices[0].message.content
            generated_code = ""
            if "```" in full_response:
                match = re.search(r"```(?:python)?\s*\n?(.*?)\n?```", full_response, re.DOTALL)
                if match: generated_code = match.group(1).strip()

            # If there's code, or the prompt looks like a modification, run the "Intent Interpreter"
            intents_data = None
            MODIFICATION_KEYWORDS = ["increase", "decrease", "change", "modify", "remove", "add", "scale", "resize", "widen", "shorten", "lengthen", "shrink", "bigger", "smaller", "taller", "wider"]
            is_modification = bool(generated_code) or any(word in request.prompt.lower() for word in MODIFICATION_KEYWORDS)
            if is_modification:
                try:
                    from app.cad_ai.routes import interpret_cad_intent, InterpretRequest
                    intent_res = await interpret_cad_intent(InterpretRequest(prompt=request.prompt, parsed_data=parsed_data or {}))
                    intents_data = intent_res
                except Exception as e:
                    print(f"Intent interpretation failed in unified flow: {e}")
                    # Fallback: create a minimal intent_response so the Apply button still shows
                    intents_data = {
                        "status": "ready_to_execute",
                        "intents": [],
                        "preview": [],
                        "secondary_modifications": [],
                        "clusters_detected": [],
                        "confidence": 0.5,
                        "alternative_interpretations": [],
                        "warnings": [f"Intent parsing error: {str(e)}"]
                    }

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
                    "intent_response": intents_data,
                    "created_at": now
                })

            return {
                "content": full_response,
                "code": generated_code,
                "intents": intents_data.get("intents") if intents_data else None,
                "preview": intents_data.get("preview") if intents_data else None,
                "intent_response": intents_data,
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

            try:
                response = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=messages,
                )
            except Exception as llm_err:
                err_str = str(llm_err).lower()
                print(f"[/generate non-CAD] LLM error type={type(llm_err).__name__} repr={repr(llm_err)}")
                if "rate_limit" in err_str or "rate limit" in err_str or "429" in err_str:
                    raise HTTPException(
                        status_code=429,
                        detail="Groq rate limit reached. You are on the free tier — wait 1 minute and try again."
                    )
                raise HTTPException(status_code=500, detail=f"LLM error: {repr(llm_err)}")

            if not response or not getattr(response, "choices", None):
                raise HTTPException(status_code=500, detail="LLM provider returned an empty response. Please retry.")

            full_response = response.choices[0].message.content or ""
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
    except HTTPException:
        raise  # re-raise already-formatted HTTP errors unchanged
    except Exception as e:
        import traceback
        print(f"Generation error type={type(e).__name__} repr={repr(e)}")
        traceback.print_exc()
        detail = repr(e) if not str(e) else str(e)
        raise HTTPException(status_code=500, detail=detail)

@app.post("/run-in-freecad")
async def run_in_freecad(request: PromptRequest):
    # The request body uses 'prompt' field to pass the code string
    code = request.prompt
    print(f"--- Attempting to send code to FreeCAD ({len(code)} bytes) ---")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(20)  # Wait for FreeCAD to recompute + save
            s.connect(("127.0.0.1", 6666))
            s.sendall(code.encode('utf-8'))
            data = s.recv(1024)
            if data == b"OK":
                return {"status": "success", "message": "Code executed in FreeCAD successfully"}
            elif data == b"ERR":
                return {"status": "error", "message": "FreeCAD reported an execution error. Check FreeCAD console for details."}
            else:
                return {"status": "partial", "message": f"Unexpected response: {data}"}
    except ConnectionRefusedError:
        raise HTTPException(status_code=503, detail="Could not connect to FreeCAD. Ensure the listener macro is running.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")



if __name__ == "__main__":
    import uvicorn
    # Enable reload for development to ensure changes are applied
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
