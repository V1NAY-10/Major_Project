"""
CAD AI Routes — New endpoints for CAD-aware conversational editing.

Endpoints:
  POST /cad/upload          — Upload & parse a CAD file, store context per session
  POST /cad/explain/{file_id} — LLM explains parsed CAD data (no code)
  GET  /cad/context/{session_id} — Retrieve stored CAD context for a session
  DELETE /cad/context/{session_id} — Clear CAD context for a session
"""

import uuid
import re
import os
import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel
from openai import OpenAI
from dotenv import load_dotenv

from .cad_context_manager import (
    parse_cad_file,
    store_cad_context,
    get_cad_context,
    get_cad_context_by_file,
    session_has_cad,
    get_explain_messages,
)
from app.cad.services.step_parser import STEPParseError

load_dotenv()

router = APIRouter(prefix="/cad", tags=["CAD AI"])

# Re-use the same OpenRouter client as main.py
api_key = os.getenv("OPENROUTER_API_KEY")
llm_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=api_key,
)

# Allowed CAD extensions
ALLOWED_EXTENSIONS = {"step", "stp", "iges", "igs", "fcstd", "brep", "stl", "obj"}


# ── Request / Response Models ───────────────────────────────────────────────

class ExplainRequest(BaseModel):
    session_id: Optional[str] = None

class InterpretRequest(BaseModel):
    prompt: str
    parsed_data: dict


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_cad_file(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(default=None),
):
    """
    Upload a CAD file, parse it, and store the result in-memory and MongoDB.
    """
    # Import db from main to persist parsing
    from main import db

    # Validate extension
    filename = file.filename or "unknown"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    # Read file content
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Parse
    file_id = str(uuid.uuid4())
    try:
        parsed_data = parse_cad_file(content, filename)
    except STEPParseError as e:
        raise HTTPException(
            status_code=422,
            detail=f"STEP parsing failed: {str(e)}",
        )
    except Exception as e:
        print(f"CAD parse error: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"CAD parsing failed: {str(e)}",
        )

    # Convert to mesh (STL)
    stl_data = None
    if ext in ("step", "stp"):
        try:
            from app.cad.services.mesh_converter import convert_step_to_stl
            stl_data = convert_step_to_stl(content)
        except Exception as e:
            print(f"Mesh conversion error: {e}")

    # Store in memory
    store_cad_context(session_id, file_id, parsed_data, stl_data, content)

    # Store in MongoDB if session exists
    if db is not None and session_id:
        try:
            from bson import ObjectId
            await db.sessions.update_one(
                {"_id": ObjectId(session_id)},
                {"$set": {
                    "cad_file_id": file_id,
                    "cad_filename": filename,
                    "cad_parsed_data": parsed_data,
                    "cad_raw_data": content,
                    "cad_stl_data": stl_data,
                    "updated_at": datetime.datetime.now(datetime.timezone.utc)
                }}
            )
        except Exception as e:
            print(f"MongoDB CAD update error: {e}")

    return {
        "file_id": file_id,
        "filename": filename,
        "session_id": session_id,
        "parsed_data": parsed_data,
    }


@router.post("/explain/{file_id}")
async def explain_cad(file_id: str, body: ExplainRequest = ExplainRequest()):
    """
    Use the LLM to generate a human-readable explanation of the parsed CAD data.

    Looks up CAD context by file_id (or session_id if provided).
    """
    if not api_key:
        raise HTTPException(status_code=500, detail="OpenRouter API key not configured.")

    # Resolve CAD context
    context = None
    if body.session_id:
        context = get_cad_context(body.session_id)
    if context is None:
        context = get_cad_context_by_file(file_id)
    if context is None:
        raise HTTPException(status_code=404, detail=f"No CAD context found for file_id '{file_id}'.")

    parsed_data = context["parsed_data"]

    # Build LLM messages
    messages = get_explain_messages(parsed_data)

    try:
        response = llm_client.chat.completions.create(
            model="google/gemini-2.0-flash-001",
            messages=messages,
        )
        explanation = response.choices[0].message.content

        return {
            "file_id": file_id,
            "explanation": explanation,
        }
    except Exception as e:
        print(f"CAD explain LLM error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/interpret")
async def interpret_cad_intent(body: InterpretRequest):
    """
    Interpret the user's intent based on the parsed CAD data and a prompt.
    Returns a structured JSON object representing the intended action.
    """
    if not api_key:
        raise HTTPException(status_code=500, detail="OpenRouter API key not configured.")

    import json
    from .services.size_ranker import rank_patterns
    from .services.preview_generator import generate_preview

    components = body.parsed_data.get("components", [])
    rankings = rank_patterns(components)

    system_prompt = (
        "You are a CAD data abstraction assistant.\n"
        "Given a user prompt and parsed CAD data, interpret the user's intent into a list of actions.\n\n"
        f"Available Size Rankings:\n{json.dumps(rankings, indent=2)}\n\n"
        "Rules for mapping relative sizes (small, large, biggest):\n"
        "- 'small' or 'smallest': ALWAYS pick the 'smallest' from the rankings for that type.\n"
        "- 'large': ALWAYS pick the 'second_largest' from the rankings. NEVER pick the 'largest' for 'large'.\n"
        "- 'biggest' or 'largest': ALWAYS pick the 'largest' from the rankings.\n"
        "- Example: If a user says 'large cylinder' and 'biggest cylinder', they MUST map to different pattern IDs.\n\n"
        "Rules for multi-command parsing:\n"
        "- You MUST split the user input into separate commands if there are multiple actions requested (e.g., separated by 'and', commas, line breaks, or multiple action verbs).\n"
        "- Each distinct command must produce exactly ONE command object in the `raw_commands` list.\n\n"
        "Return a JSON object with a single key `raw_commands`: a list of command objects.\n"
        "Each command object must have:\n"
        "{\n"
        "  \"target_pattern\": \"string (exact ID from rankings, e.g., 'comp_1')\",\n"
        "  \"action\": \"string (e.g., 'increase_diameter', 'decrease_height')\",\n"
        "  \"value\": \"number (You MUST deduce the exact new value. Read the original diameter/radius from the Parsed CAD Data. If the prompt just says 'increase', you must do the math to provide a real numerical value. NEVER return null or a string like 'default').\",\n"
        "  \"reason\": \"string (brief explanation of why this target was chosen)\"\n"
        "}\n"
        "Return ONLY the JSON object, with no markdown wrapping or additional text."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Parsed CAD Data:\n{json.dumps(body.parsed_data, indent=2)}\n\nUser Prompt: {body.prompt}"}
    ]

    try:
        response = llm_client.chat.completions.create(
            model="google/gemini-2.0-flash-001",
            messages=messages,
            response_format={"type": "json_object"}
        )
        intent_json = response.choices[0].message.content
        
        if intent_json.startswith("```json"):
            intent_json = intent_json[7:-3].strip()
        elif intent_json.startswith("```"):
            intent_json = intent_json[3:-3].strip()
            
        raw_output = json.loads(intent_json)
        raw_commands = raw_output.get("raw_commands", [])

        # Conflict resolution and Missing value handling
        intents_dict = {}
        warnings = []
        seen_warnings = set()
        for cmd in raw_commands:
            target_id = cmd.get("target_pattern")
            if not target_id:
                continue

            if target_id in intents_dict and target_id not in seen_warnings:
                warnings.append({
                    "warning": f"Conflicting operations on same pattern ({target_id}). Last command applied."
                })
                seen_warnings.add(target_id)
            intents_dict[target_id] = cmd

        # Formatting
        final_intents = []
        previews = []
        for target_id, cmd in intents_dict.items():
            # Get human readable label
            comp = next((c for c in components if c["id"] == target_id), None)
            
            # Fallback if the LLM hallucinated a pattern_ prefix
            if not comp and target_id.startswith("pattern_"):
                comp = next((c for c in components if c.get("pattern_id") == target_id), None)
                if comp:
                    target_id = comp["id"]  # Fix it so the rest of the flow uses the right ID
                    
            if not comp:
                raise ValueError(f"Could not resolve target ID '{target_id}' to any known component.")
                
            label = target_id
            if comp:
                ftype = comp.get("type", "").replace("_pattern", "")
                label = ftype.capitalize() + ("s" if comp.get("count", 1) > 1 else "")
                if "role" in comp:
                    label = f"{label} ({comp['role']})"

            # Add full pattern data
            cmd["target_pattern"] = target_id
            cmd["target_label"] = label
            cmd["pattern_data"] = comp
            
            final_intents.append(cmd)
            previews.append(generate_preview(cmd))

        return {
            "intents": final_intents,
            "preview": previews,
            "warnings": warnings
        }

    except Exception as e:
        print(f"CAD interpret LLM error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/context/{session_id}")
async def get_session_cad_context(session_id: str):
    """Retrieve the stored CAD context for a session."""
    context = get_cad_context(session_id)
    if context is None:
        raise HTTPException(
            status_code=404,
            detail=f"No CAD context found for session '{session_id}'.",
        )
    return context


@router.delete("/context/{session_id}")
async def clear_session_cad_context(session_id: str):
    """Clear the stored CAD context for a session."""
    from .cad_memory_store import cad_store

    removed = cad_store.remove(session_id)
    return {"cleared": removed, "session_id": session_id}

class ModifyModelRequest(BaseModel):
    file_id: str
    intents: list

@router.post("/modify-model")
async def modify_cad_model(body: ModifyModelRequest):
    """
    Apply modifications to the real geometry based on intents,
    re-mesh, and return the new mesh URL.
    """
    context = get_cad_context_by_file(body.file_id)
    if not context or "raw_cad_data" not in context or not context["raw_cad_data"]:
        raise HTTPException(status_code=404, detail="CAD data not found.")
        
    try:
        from app.cad.services.geometry_modifier import modify_geometry_and_export
        import concurrent.futures
        
        fallback_stl = context.get("stl_data")
        raw_data = context["raw_cad_data"]
        intents = body.intents

        # ThreadPoolExecutor to enforce 5-second hard limit
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(modify_geometry_and_export, raw_data, intents, fallback_stl)
            try:
                new_stl, warnings = future.result(timeout=30.0)
            except concurrent.futures.TimeoutError:
                print("[CAD Modifier] TIMEOUT: Modification exceeded 30 seconds.")
                # We do not crash the server, but we return a safe fallback response
                return {
                    "error": "Geometry modification timeout",
                    "reason": "The requested modification took too long and was aborted.",
                    "fallback": True,
                    "mesh_url": f"/cad/model/{body.file_id}" # return original
                }
        
        # Save the new STL into the cache so /model/{file_id} returns it
        session_id = context["session_id"]
        store_cad_context(
            session_id, 
            body.file_id, 
            context["parsed_data"], 
            new_stl, 
            context["raw_cad_data"]
        )

        # Persist the new STL to MongoDB
        from main import db
        if db is not None and session_id:
            try:
                from bson import ObjectId
                await db.sessions.update_one(
                    {"_id": ObjectId(session_id)},
                    {"$set": {
                        "cad_stl_data": new_stl,
                        "updated_at": datetime.datetime.now(datetime.timezone.utc)
                    }}
                )
            except Exception as e:
                print(f"MongoDB CAD modify update error: {e}")
        
        import uuid
        # Return a cache-busting URL so the frontend viewer reloads
        return {
            "mesh_url": f"/cad/model/{body.file_id}?t={uuid.uuid4().hex[:8]}",
            "warnings": warnings
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        # Fail-safe return
        return {
            "error": "Modification failed",
            "reason": str(e),
            "fallback": True,
            "mesh_url": f"/cad/model/{body.file_id}"
        }

@router.get("/model/{file_id}")
async def get_cad_mesh(file_id: str):
    """
    Return the STL or OBJ file converted from the uploaded CAD file.
    """
    context = get_cad_context_by_file(file_id)
    if not context or "stl_data" not in context or not context["stl_data"]:
        raise HTTPException(status_code=404, detail="Mesh data not found for this file.")
    
    return Response(content=context["stl_data"], media_type="application/octet-stream")

class GenerateScriptRequest(BaseModel):
    intents: list

@router.post("/generate-script")
async def generate_freecad_script(body: GenerateScriptRequest):
    """
    Generate FreeCAD Python code based on the provided intents.
    Does NOT execute the code.
    """
    if not body.intents:
        return {"script": "# No intents provided\n"}

    script = [
        "import FreeCAD as App",
        "import Part",
        "",
        "# ── Auto-generated FreeCAD Macro ──",
        "doc = App.ActiveDocument",
        "if not doc:",
        "    doc = App.newDocument('IntentModifications')",
        "",
        "try:"
    ]

    for intent in body.intents:
        action = intent.get("action", "")
        target = intent.get("target_pattern", "")
        val = intent.get("value", "")
        
        script.append(f"    # Intent: {action} on {target} to {val}")
        
        # Generic safe FreeCAD mock execution
        script.append(f"    obj = doc.getObject('{target}')")
        script.append(f"    if obj:")
        if "diameter" in action or "radius" in action:
            script.append(f"        obj.Radius = float({val}) / 2.0")
        elif "length" in action or "height" in action:
            script.append(f"        obj.Length = float({val})")
        else:
            script.append(f"        pass # Custom modifier for {action}")
        script.append(f"    else:")
        script.append(f"        print('Warning: Object {target} not found')")
        script.append("")
        
    script.append("    doc.recompute()")
    script.append("except Exception as e:")
    script.append("    print(f'Macro Error: {e}')")

    return {"script": "\n".join(script)}

