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
from typing import Optional, Dict, Any

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
    store_cad_blobs,
)
from app.cad.services.parsers import STEPParseError

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
        print(f"CAD parsing warning: {e}. Falling back to generic parsing.")
        from .cad_context_manager import _parse_generic
        parsed_data = _parse_generic(content, filename)
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

    # Automatically send to FreeCAD if listener is running
    import tempfile, socket, os
    with tempfile.NamedTemporaryFile(delete=False, suffix=".step") as tmp:
        tmp.write(content)
        temp_path = tmp.name.replace('\\', '/')

    fc_script = f"""import FreeCAD as App
import ImportGui
import FreeCADGui as Gui
doc = App.ActiveDocument
if not doc:
    doc = App.newDocument('CAD_Session')
try:
    # Clear existing objects safely (collect names first to avoid iterator invalidation)
    obj_names = [o.Name for o in doc.Objects]
    for name in obj_names:
        try:
            doc.removeObject(name)
        except Exception:
            pass
    doc.recompute()
    ImportGui.insert(r'{temp_path}', doc.Name)
    doc.recompute()
    if Gui.ActiveDocument and Gui.ActiveDocument.ActiveView:
        Gui.SendMsgToActiveView("ViewFit")
except Exception as e:
    print(e)
"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(("127.0.0.1", 6666))
            s.sendall(fc_script.encode('utf-8'))
            s.recv(1024)
    except:
        pass # FreeCAD listener not running

    # Store in MongoDB if session exists
    if db is not None and session_id:
        try:
            from bson import ObjectId
            
            # If data is large, use GridFS
            update_data: Dict[str, Any] = {
                "cad_file_id": file_id,
                "cad_filename": filename,
                "cad_parsed_data": parsed_data,
                "updated_at": datetime.datetime.now(datetime.timezone.utc)
            }
            
            MAX_DOC_SIZE = 15 * 1024 * 1024 # 15MB limit
            total_blob_size = len(content) + (len(stl_data) if stl_data else 0)
            
            if total_blob_size > MAX_DOC_SIZE:
                print(f"CAD blobs too large ({total_blob_size} bytes), using GridFS.")
                stl_fs_id, raw_fs_id = await store_cad_blobs(file_id, stl_data, content)
                update_data["cad_stl_fs_id"] = stl_fs_id
                update_data["cad_raw_fs_id"] = raw_fs_id
            else:
                update_data["cad_raw_data"] = content
                update_data["cad_stl_data"] = stl_data

            await db.sessions.update_one(
                {"_id": ObjectId(session_id)},
                {"$set": update_data}
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
        context = await get_cad_context(body.session_id)
    if context is None:
        context = await get_cad_context_by_file(file_id)
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
        
        if not response or not getattr(response, "choices", None):
            raise HTTPException(status_code=500, detail="LLM provider returned an empty response. Please retry.")
            
        explanation = response.choices[0].message.content

        return {
            "file_id": file_id,
            "explanation": explanation,
        }
    except Exception as e:
        print(f"CAD explain LLM error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def interpret_cad_intent_sync(body: InterpretRequest):
    from .services.structure_analyzer import CompositeStructureAnalyzer
    from .services.intent_disambiguator import IntentDisambiguator
    from .services.confirmation_manager import ConfirmationManager
    from .services.llm_interpreter import LLM_SYSTEM_PROMPT
    import json
    
    components = body.parsed_data.get("components", [])
    
    # Step 1: Structure analysis
    analyzer = CompositeStructureAnalyzer(distance_threshold=150.0)
    clusters = analyzer.analyze(components)
    
    # Step 2: Use LLM to decode the structured intent
    clusters_context = json.dumps([{"cluster_id": c.cluster_id, "component_type": c.component_type, "member_ids": c.member_ids, "primary_geometry": c.primary_geometry, "secondary_geometries": c.secondary_geometries, "confidence": c.confidence, "editing_hints": c.editing_hints, "spatial_location": getattr(c, "spatial_location", "unknown")} for c in clusters], indent=2)
    
    # Since the guide uses llm to decode structured intent first, let's call the LLM:
    messages = [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {"role": "user", "content": f"User prompt: {body.prompt}\n\nAvailable composite clusters:\n{clusters_context}\n\nAvailable parsed features:\n{json.dumps(components, indent=2)}\n\nRespond with JSON following the format specified in your system prompt."}
    ]
    
    response = llm_client.chat.completions.create(
        model="google/gemini-2.0-flash-001",
        messages=messages,
    )
    
    intent_json = response.choices[0].message.content or "{}"
    
    # Strip markdown code fences if present
    if "```json" in intent_json:
        import re as _re
        m = _re.search(r"```json\s*(.*?)\s*```", intent_json, _re.DOTALL)
        intent_json = m.group(1) if m else intent_json
    elif "```" in intent_json:
        import re as _re
        m = _re.search(r"```\s*(.*?)\s*```", intent_json, _re.DOTALL)
        intent_json = m.group(1) if m else intent_json
    
    # Find the first JSON object in the response (handles extra text around it)
    import re as _re
    json_match = _re.search(r"\{.*\}", intent_json, _re.DOTALL)
    intent_json = json_match.group(0) if json_match else "{}"
    
    try:
        raw_output = json.loads(intent_json)
    except json.JSONDecodeError:
        print(f"[Interpret] Failed to parse LLM JSON: {intent_json[:200]}")
        raw_output = {}
    
    # Step 3: We can use the intent_disambiguator here if we want to run the python logic, but the guide's LLM prompt returns exactly the json structure we need. Let's return what LLM gave, formatted.
    
    # Format the output so main.py doesn't break
    # main.py expects: intents_data.get("intents") and intents_data.get("preview")
    from .services.ai_utils import generate_intent_preview as generate_preview
    
    final_intents = []
    previews = []
    
    raw_intents = raw_output.get("intents", [])
    for cmd in raw_intents:
        target_id = cmd.get("target_pattern")
        comp = next((c for c in components if c.get("id") == target_id), None)
        
        if comp:
            ftype = comp.get("type", "").replace("_pattern", "")
            label = ftype.capitalize() + ("s" if comp.get("count", 1) > 1 else "")
            if "role" in comp:
                label = f"{label} ({comp['role']})"
            cmd["target_label"] = label
            cmd["pattern_data"] = comp
            try:
                previews.append(generate_preview(cmd))
            except Exception:
                pass
        else:
            # No matching component found, still include the intent
            cmd.setdefault("target_label", target_id or "Unknown")
            cmd.setdefault("pattern_data", {})
            
        final_intents.append(cmd)
        
    return {
        "status": raw_output.get("status", "ready_to_execute"),
        "intents": final_intents,
        "preview": previews,
        "secondary_modifications": raw_output.get("secondary_modifications", []),
        "clusters_detected": raw_output.get("clusters_involved", []),
        "confidence": raw_output.get("confidence", 1.0),
        "alternative_interpretations": raw_output.get("alternatives", []),
        "warnings": []
    }

@router.post("/interpret")
async def interpret_cad_intent(body: InterpretRequest):
    """
    Interpret the user's intent based on the parsed CAD data and a prompt.
    Returns a structured JSON object representing the intended action.
    """
    if not api_key:
        raise HTTPException(status_code=500, detail="OpenRouter API key not configured.")
        
    try:
        return interpret_cad_intent_sync(body)
    except Exception as e:
        print(f"CAD interpret error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/context/{session_id}")
async def get_session_cad_context(session_id: str):
    """Retrieve the stored CAD context for a session."""
    context = await get_cad_context(session_id)
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
    context = await get_cad_context_by_file(body.file_id)
    if not context or "raw_cad_data" not in context or not context["raw_cad_data"]:
        raise HTTPException(status_code=404, detail="CAD data not found.")
        
    try:
        from app.cad.services.geometry_modifier import modify_geometry_and_export
        import concurrent.futures
        
        fallback_stl = context.get("stl_data")
        raw_data = context["raw_cad_data"]
        intents = body.intents

        if not intents:
            print("[CAD Modifier] No intents provided — returning original model URL.")
            return {
                "mesh_url": f"/cad/model/{body.file_id}",
                "warnings": ["No modification intents were parsed. The model is unchanged."]
            }

        print(f"[CAD Modifier] Applying {len(intents)} intent(s): {[i.get('action') for i in intents]}")

        # ThreadPoolExecutor to enforce hard limit
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(modify_geometry_and_export, raw_data, intents, fallback_stl)
            try:
                new_stl, new_step, warnings = future.result(timeout=30.0)
            except concurrent.futures.TimeoutError:
                print("[CAD Modifier] TIMEOUT: Modification exceeded 30 seconds.")
                return {
                    "error": "Geometry modification timeout",
                    "reason": "The requested modification took too long and was aborted.",
                    "fallback": True,
                    "mesh_url": f"/cad/model/{body.file_id}"
                }
        
        # Save the new STL into the cache so /model/{file_id} returns it
        session_id = context["session_id"]
        store_cad_context(
            session_id, 
            body.file_id, 
            context["parsed_data"], 
            new_stl, 
            new_step
        )

        # Persist the new STL to MongoDB
        from main import db
        if db is not None and session_id:
            try:
                from bson import ObjectId
                
                update_data = {
                    "updated_at": datetime.datetime.now(datetime.timezone.utc)
                }
                
                # Update STL
                if len(new_stl) > 15 * 1024 * 1024:
                    stl_fs_id, _ = await store_cad_blobs(body.file_id, stl_data=new_stl)
                    update_data["cad_stl_fs_id"] = stl_fs_id
                    update_data["cad_stl_data"] = None # Clear old if existed
                else:
                    update_data["cad_stl_data"] = new_stl
                    update_data["cad_stl_fs_id"] = None # Clear old if existed
                    
                # Update STEP (raw_cad_data)
                if len(new_step) > 15 * 1024 * 1024:
                    _, raw_fs_id = await store_cad_blobs(body.file_id, raw_cad_data=new_step)
                    update_data["cad_raw_fs_id"] = raw_fs_id
                    update_data["cad_raw_data"] = None # Clear old if existed
                else:
                    update_data["cad_raw_data"] = new_step
                    update_data["cad_raw_fs_id"] = None # Clear old if existed

                await db.sessions.update_one(
                    {"_id": ObjectId(session_id)},
                    {"$set": update_data}
                )
            except Exception as e:
                print(f"MongoDB CAD modify update error: {e}")
        
        import uuid
        
        # Also sync modifications with FreeCAD if running
        try:
            import socket, tempfile, os
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".step")
            tmp_path = tmp_path.replace('\\', '/')
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(new_step)
            
            fc_script = f"""import FreeCAD as App
import ImportGui
import FreeCADGui as Gui
doc = App.ActiveDocument
if not doc:
    doc = App.newDocument('CAD_Session')
try:
    # Clear existing objects safely (collect names first to avoid iterator invalidation)
    obj_names = [o.Name for o in doc.Objects]
    for name in obj_names:
        try:
            doc.removeObject(name)
        except Exception:
            pass
    doc.recompute()
    ImportGui.insert(r'{tmp_path}', doc.Name)
    doc.recompute()
    if Gui.ActiveDocument and Gui.ActiveDocument.ActiveView:
        Gui.SendMsgToActiveView("ViewFit")
except Exception as e:
    print(e)
"""
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", 6666))
                s.sendall(fc_script.encode('utf-8'))
                s.recv(1024)
        except Exception as e:
            print(f"Failed to sync modification with FreeCAD: {e}")

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

@router.get("/download/{file_id}")
async def download_cad_mesh(file_id: str):
    """
    Download the modified STL file.
    """
    context = await get_cad_context_by_file(file_id)
    if not context or "stl_data" not in context or not context["stl_data"]:
        raise HTTPException(status_code=404, detail="Mesh data not found.")
    
    filename = context.get("cad_filename", f"modified_{file_id}.stl")
    if "." in filename:
        filename = filename.rsplit(".", 1)[0] + ".stl"
    else:
        filename += ".stl"
        
    return Response(
        content=context["stl_data"], 
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@router.get("/model/{file_id}")
async def get_cad_mesh(file_id: str):
    """
    Return the STL or OBJ file converted from the uploaded CAD file.
    """
    context = await get_cad_context_by_file(file_id)
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

class GeometryReasoningRequest(BaseModel):
    components: Optional[list] = None
    primitives: Optional[list] = None
    instruction: str

@router.post("/geometry-reasoning")
async def apply_geometry_reasoning(body: GeometryReasoningRequest):
    """
    Applies deterministic geometry reasoning based on the provided components
    and instructions.
    """
    from .services.geometry_reasoning import GeometryReasoningEngine
    engine = GeometryReasoningEngine()
    
    comp_list = body.components if body.components is not None else body.primitives or []

    try:
        result = engine.process_model(comp_list, body.instruction)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/cad/vocab/{session_id}")
async def define_component_term(session_id: str, term: str, cluster_id: str):
    """
    Frontend: User right-clicks a component, says "I call this the arm"
    Backend: Learns the term for this session
    """
    vocab = SessionVocabulary(db)
    await vocab.define_term(session_id, term, cluster_id)
    return {"status": "learned", "term": term, "cluster_id": cluster_id}
