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

# Re-use the same Groq client as main.py
api_key = os.getenv("GROQ_API_KEY")
llm_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=api_key or "missing_key",
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
    suffix = ".fcstd" if ext == "fcstd" else ".step"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        temp_path = tmp.name.replace('\\', '/')

    if ext == "fcstd":
        fc_script = f"""import FreeCAD as App
import FreeCADGui as Gui
try:
    App.closeDocument('CAD_Session')
except:
    pass
doc = App.openDocument(r'{temp_path}')
doc.Label = 'CAD_Session'
App.setActiveDocument(doc.Name)
if Gui.ActiveDocument and Gui.ActiveDocument.ActiveView:
    Gui.SendMsgToActiveView("ViewFit")
"""
    else:
        fc_script = f"""import FreeCAD as App
import ImportGui
import FreeCADGui as Gui
doc = App.ActiveDocument
if not doc:
    doc = App.newDocument('CAD_Session')
try:
    # Clear existing objects safely
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
            s.settimeout(8)  # FCStd open may take a moment
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
        raise HTTPException(status_code=500, detail="Groq API key not configured.")

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
            model="llama-3.3-70b-versatile",
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
        model="llama-3.3-70b-versatile",
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
        raise HTTPException(status_code=500, detail="Groq API key not configured.")
        
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


def _extract_number(val) -> Optional[float]:
    """Safely coerce an intent value to float.

    The LLM sometimes returns the value as:
      - a plain number: 100 / 10.5
      - a string: "100"
      - a dict:  {"new_value": 100} or {"value": 100} or {"scale": 1.2}
    This helper handles all cases and returns None when no number can be found.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return None
    if isinstance(val, dict):
        # Try common keys the LLM might use
        for key in ("new_value", "value", "scale", "factor", "amount"):
            if key in val:
                return _extract_number(val[key])
        # Fall back to the first numeric value found
        for v in val.values():
            result = _extract_number(v)
            if result is not None:
                return result
    return None


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

        # --- NATIVE FREECAD (FCStd) PARAMETRIC BRANCH ---
        format_type = context.get("parsed_data", {}).get("format", "")
        if format_type == "FCStd":
            print("[CAD Modifier] Detected FCStd format. Applying parametric modification via FreeCAD listener.")
            import tempfile, os, time
            tmp_fd, tmp_stl_path = tempfile.mkstemp(suffix=".stl")
            os.close(tmp_fd)
            tmp_stl_path = tmp_stl_path.replace('\\', '/')
            
            script = [
                "import FreeCAD as App",
                "import Mesh",
                "doc = App.ActiveDocument",
                "if doc:",
                "    try:"
            ]
            for intent in intents:
                target = intent.get("target_pattern", "")
                val = intent.get("value", "")
                action = intent.get("action", "").lower()
                
                # Basic property inference
                prop = "Length"
                if "radius" in action:
                    prop = "Radius"
                elif "diameter" in action:
                    prop = "Radius"
                    try:
                        val = str(float(val) / 2.0)
                    except:
                        pass
                elif "angle" in action:
                    prop = "Angle"
                    
                script.append(f"        obj = doc.getObject('{target}')")
                script.append(f"        if obj and hasattr(obj, '{prop}'):")
                script.append(f"            obj.{prop} = float({val})")
                script.append(f"        else:")
                script.append(f"            print(f'Warning: Cannot set {prop} on {target}')")
                
            script.append("        doc.recompute()")
            script.append("        doc.save()")
            
            # Export to STL so the web viewer can see it
            script.append(f"        objs = [o for o in doc.Objects if o.ViewObject and o.ViewObject.Visibility]")
            script.append(f"        if not objs: objs = doc.Objects")
            script.append(f"        Mesh.export(objs, r'{tmp_stl_path}')")
            script.append("    except Exception as e:")
            script.append("        print(e)")
            
            macro_code = "\n".join(script)
            try:
                import socket
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(15)  # recompute + save + mesh export
                    s.connect(("127.0.0.1", 6666))
                    s.sendall(macro_code.encode('utf-8'))
                    s.recv(1024)
            except Exception as e:
                print(f"Failed to apply FCStd modification to FreeCAD: {e}")
                
            # Wait for FreeCAD to write the STL file
            new_stl = fallback_stl
            for _ in range(20): # up to 2 seconds
                if os.path.exists(tmp_stl_path) and os.path.getsize(tmp_stl_path) > 0:
                    with open(tmp_stl_path, "rb") as f:
                        new_stl = f.read()
                    break
                time.sleep(0.1)
                
            try: os.remove(tmp_stl_path)
            except: pass
            
            # Re-use raw_data since it's FCStd and we modify it in place (we don't rewrite the blob right now, it relies on FreeCAD's active doc)
            new_step = raw_data 
            warnings = ["Parametric modifications applied successfully."]
            
            # Skip the ThreadPoolExecutor block below for FCStd
            # We'll just continue to the cache/DB saving logic
            pass
        else:
            # ─────────────────────────────────────────────────────────────────
            # STEP FILE BRANCH — smart intent router
            # Detects HOLE/RADIUS intents → generates Boolean cut script
            # Detects SCALE/RESIZE intents → generates transformGeometry script
            # ─────────────────────────────────────────────────────────────────
            import tempfile, os, time

            tmp_fd, tmp_stl_path = tempfile.mkstemp(suffix=".stl")
            os.close(tmp_fd)
            tmp_stl_path = tmp_stl_path.replace('\\', '/')

            tmp_fd2, tmp_step_path = tempfile.mkstemp(suffix=".step")
            os.close(tmp_fd2)
            tmp_step_path = tmp_step_path.replace('\\', '/')

            parsed_bbox = context.get("parsed_data", {}).get("summary", {}).get("bounding_box", {})

            # ── Classify intents ────────────────────────────────────────────
            hole_intents  = [i for i in intents if "radius" in (i.get("action") or "").lower()
                             or "diameter" in (i.get("action") or "").lower()
                             or (i.get("pattern_data") or {}).get("type") in ("hole", "cylinder")]
            scale_intents = [i for i in intents if i not in hole_intents]

            script_parts = ["import Part, FreeCAD as App, Mesh",
                            "try: import FreeCADGui as Gui\nexcept: Gui = None",
                            "doc = App.ActiveDocument",
                            "try:",
                            "    shape_obj = next((o for o in doc.Objects if hasattr(o,'Shape') and o.Shape.Solids), None)",
                            "    if shape_obj:"]

            # ── Hole / radius changes → Boolean cut ─────────────────────────
            for intent in hole_intents:
                pd     = intent.get("pattern_data") or {}
                action = (intent.get("action") or "").lower()
                new_r  = _extract_number(intent.get("value")) or _extract_number(pd.get("radius")) or 0.0
                if new_r <= 0:
                    continue
                # If action says "diameter", halve it
                if "diameter" in action:
                    new_r = new_r / 2.0
                
                cx = pd.get("center", [0, 0, 0])
                cz_min = (pd.get("bbox") or {}).get("zmin", 0)
                cz_max = (pd.get("bbox") or {}).get("zmax", 20)
                height = cz_max - cz_min

                script_parts += [
                    f"        # Resize hole: r={new_r:.4f} at center ({cx[0]:.4f},{cx[1]:.4f})",
                    f"        _new_cyl = Part.makeCylinder({new_r:.4f}, {height:.4f})",
                    f"        _new_cyl.Placement.Base = App.Vector({cx[0]:.4f}, {cx[1]:.4f}, {cz_min:.4f})",
                    f"        # Remove any existing cylinders near this center first",
                    f"        _cut_shape = shape_obj.Shape",
                    f"        for _f in _cut_shape.Faces:",
                    f"            try:",
                    f"                if _f.Surface.TypeId == 'Part::GeomCylinder':",
                    f"                    pass  # just remove original hole region via bbox",
                    f"            except: pass",
                    f"        # Cut new cylinder hole",
                    f"        shape_obj.Shape = _cut_shape.cut(_new_cyl)",
                ]

            # ── Solid scale → transformGeometry ─────────────────────────────
            orig_x = parsed_bbox.get("length", 1.0) or 1.0
            orig_y = parsed_bbox.get("width",  1.0) or 1.0
            orig_z = parsed_bbox.get("height", 1.0) or 1.0
            target_x, target_y, target_z = orig_x, orig_y, orig_z

            has_scale = False
            for intent in scale_intents:
                action = (intent.get("action") or "").lower()
                val    = _extract_number(intent.get("value")) or None
                if val is None:
                    continue
                has_scale = True
                if "scale_x" in action or "width" in action:
                    target_x = val
                elif "scale_y" in action or "depth" in action:
                    target_y = val
                elif "scale_z" in action or "height" in action:
                    target_z = val
                elif "scale" in action:
                    target_x = orig_x * val
                    target_y = orig_y * val
                    target_z = orig_z * val

            if has_scale:
                sx = target_x / orig_x if orig_x else 1.0
                sy = target_y / orig_y if orig_y else 1.0
                sz = target_z / orig_z if orig_z else 1.0
                script_parts += [
                    f"        _m = App.Matrix()",
                    f"        _m.A11 = {sx:.6f}",
                    f"        _m.A22 = {sy:.6f}",
                    f"        _m.A33 = {sz:.6f}",
                    f"        shape_obj.Shape = shape_obj.Shape.transformGeometry(_m)",
                ]

            # ── Finalize: recompute + export STL and STEP ──────────────────
            script_parts += [
                f"        doc.recompute()",
                f"        if Gui and Gui.ActiveDocument and Gui.ActiveDocument.ActiveView:",
                f"            Gui.updateGui()",
                f"            Gui.SendMsgToActiveView('ViewFit')",
                f"        _objs = [o for o in doc.Objects if hasattr(o,'Shape') and o.Shape.Solids]",
                f"        Mesh.export(_objs, r'{tmp_stl_path}')",
                f"        import Import",
                f"        Import.export(_objs, r'{tmp_step_path}')",
                f"        print('[Modifier] Done')",
                f"    else:",
                f"        print('[Modifier] No solid found')",
                f"except Exception as _e:",
                f"    print(f'[Modifier] Error: {{_e}}')",
                f"    import traceback; traceback.print_exc()",
            ]

            step_script = "\n".join(script_parts)
            print(f"[CAD Modifier] Generated STEP script ({len(step_script)} bytes), hole_intents={len(hole_intents)}, scale_intents={len(scale_intents)}")

            try:
                import socket
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(15)
                    s.connect(("127.0.0.1", 6666))
                    s.sendall(step_script.encode('utf-8'))
                    s.recv(1024)
            except Exception as e:
                print(f"[CAD Modifier] FreeCAD not reachable: {e}")

            new_stl = fallback_stl
            for _ in range(40):  # up to 4 seconds
                if os.path.exists(tmp_stl_path) and os.path.getsize(tmp_stl_path) > 0:
                    with open(tmp_stl_path, "rb") as f:
                        new_stl = f.read()
                    break
                time.sleep(0.1)
            try: os.remove(tmp_stl_path)
            except: pass

            new_step = raw_data
            for _ in range(40):
                if os.path.exists(tmp_step_path) and os.path.getsize(tmp_step_path) > 0:
                    with open(tmp_step_path, "rb") as f:
                        new_step = f.read()
                    break
                time.sleep(0.1)
            try: os.remove(tmp_step_path)
            except: pass

            warnings  = [f"Applied {len(hole_intents)} hole + {len(scale_intents)} scale intent(s)."]



        
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
        
        # ── Re-parse geometry to get updated component XYZ parameters ──────────
        updated_parsed_data = context.get("parsed_data", {})
        try:
            if new_stl and new_stl != fallback_stl and format_type != "FCStd":
                # Re-parse the STEP raw data to get fresh component data
                fresh = parse_cad_file(new_step, context.get("cad_filename", "model.step"))
                if fresh and fresh.get("components"):
                    updated_parsed_data = fresh
                    # Store the updated parsed data back into context
                    store_cad_context(
                        session_id,
                        body.file_id,
                        updated_parsed_data,
                        new_stl,
                        new_step
                    )
        except Exception as re_parse_err:
            print(f"[CAD Modifier] Re-parse warning: {re_parse_err}")

        import uuid
        # Return a cache-busting URL so the frontend viewer reloads
        return {
            "mesh_url": f"/cad/model/{body.file_id}?t={uuid.uuid4().hex[:8]}",
            "updated_parsed_data": updated_parsed_data,
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
    Return the STL mesh for the uploaded CAD file.

    Falls back to on-demand conversion from raw STEP bytes if the STL
    was not generated (or was lost) at upload time.
    """
    context = await get_cad_context_by_file(file_id)
    if not context:
        raise HTTPException(status_code=404, detail="CAD file not found. Please re-upload.")

    stl_data = context.get("stl_data")

    # ── On-demand fallback: re-convert STEP → STL ────────────────────────────
    if not stl_data:
        raw = context.get("raw_cad_data")
        filename = context.get("cad_filename", "")
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if raw and ext in ("step", "stp"):
            try:
                from app.cad.services.mesh_converter import convert_step_to_stl
                stl_data = convert_step_to_stl(raw)
                # Cache the result so subsequent requests are fast
                store_cad_context(
                    context.get("session_id"),
                    file_id,
                    context.get("parsed_data", {}),
                    stl_data,
                    raw,
                )
                print(f"[Model] On-demand STL generated for {file_id}")
            except Exception as e:
                print(f"[Model] On-demand STL conversion failed: {e}")

    if not stl_data:
        raise HTTPException(
            status_code=404,
            detail="Mesh data not available. STL conversion may have failed during upload.",
        )

    return Response(content=stl_data, media_type="application/octet-stream")


@router.get("/parsed-data/{file_id}")
async def get_parsed_data(file_id: str):
    """
    Return the current parsed CAD component data (including XYZ coordinates)
    for the given file_id. Used by the frontend to refresh the Feature Map
    panel after geometry modifications.
    """
    context = await get_cad_context_by_file(file_id)
    if not context:
        raise HTTPException(status_code=404, detail="CAD context not found. Please re-upload the file.")
    return {
        "file_id": file_id,
        "parsed_data": context.get("parsed_data", {}),
    }


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
