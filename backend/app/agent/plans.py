"""
Pending plans: previews (dry runs) of one or more edits, applied as one version only when the user approves.

  preview(meta, steps)  run the steps without keeping them: one step uses the engine's dry run (transaction aborted);
                        several steps are applied for real, the result is meshed, then the head version is restored —
                        either way the model ends up exactly as it was. The plan waits in meta["pending"].
  apply(meta)           run the pending steps for real; if step N fails, everything is rolled back to the version the
                        plan started from; otherwise the steps become one new version.
  cancel(meta)          drop the pending plan.
"""

import copy
import uuid

from .. import cad_service as cad
from .. import freecad_client as fc
from . import tools

# Edits big enough that the agent shows a preview instead of applying them straight away.
LARGE_TOOLS = {"shell_object", "split_object", "scale_object", "mirror_object", "draft_faces"}


def is_large(steps):
    edits = [s for s in steps if s["tool"] in tools.EDIT_TOOLS]
    if len(edits) >= 3:
        return True
    for s in edits:
        a = s.get("args") or {}
        if s["tool"] in LARGE_TOOLS or (s["tool"] == "add_primitive" and a.get("operation") == "replace") \
                or (s["tool"] == "remove_features" and len(a.get("features") or []) >= 3) \
                or (s["tool"] == "pattern_feature" and int(a.get("count") or 0) >= 6):
            return True
    return False


def label(step):
    a = step.get("args") or {}
    keys = [f"{k}={v}" for k, v in a.items() if not isinstance(v, (dict, list))][:4]
    lists = [f"{k}={','.join(map(str, v))}" for k, v in a.items() if isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v)][:1]
    return f"{step['tool']}(" + ", ".join(keys + lists) + ")"


def _record(rec):
    if rec is None:
        return None
    return {k: rec.get(k) for k in ("tool", "ok", "message", "error", "verified", "warnings", "side_effects")}


async def preview(meta, steps, summary="", source="agent"):
    """Dry-run the steps and store them as meta["pending"]. Returns the pending plan."""
    steps = [{"tool": s["tool"], "args": dict(s.get("args") or {})} for s in steps]
    bad = [s["tool"] for s in steps if s["tool"] not in tools.EDIT_TOOLS]
    if not steps or bad:
        raise cad.CadError("A plan lists edit tool calls only" + (f" (not {', '.join(bad)})" if bad else ""))
    sid = meta["session_id"]
    head = meta["head"]
    records, ok, error = [], True, None
    if len(steps) == 1:
        result, rec = await tools.execute(meta, steps[0]["tool"], steps[0]["args"], preview=True)
        records.append(_record(rec))
        ok = bool(result.get("ok"))
        error = result.get("error")
    else:
        reg0, info0 = copy.deepcopy(meta["registry"]), meta["info"]
        try:
            for i, s in enumerate(steps, 1):
                result, rec = await tools.execute(meta, s["tool"], s["args"])
                records.append(_record(rec))
                if not result.get("ok"):
                    ok, error = False, f"step {i} ({label(s)}) failed: {result.get('error')}"
                    break
            if ok:
                await fc.call("export", session_id=sid, work_path=cad._p(cad.work_path(sid)), fmt="stl",
                              path=cad._p(cad.preview_mesh_path(sid)))
        finally:
            meta["registry"], meta["info"] = reg0, info0
            meta["dirty"] = True
            await cad.discard_uncommitted(meta)  # back to the head version, exactly
    pending = {"id": uuid.uuid4().hex[:10], "summary": summary or "; ".join(label(s) for s in steps),
               "steps": [{**s, "label": label(s)} for s in steps], "records": records, "ok": ok, "error": error,
               "base_version": head, "source": source, "created_at": cad._now(),
               "mesh_url": f"/cad/preview-mesh/{sid}?v={head}&p=" if ok else None}
    if ok:
        pending["mesh_url"] += pending["id"]
    meta["pending"] = pending
    cad.save(meta)
    return pending


async def apply(meta, plan_id=None):
    """Apply the pending plan as one version. Returns (reply text, records, ok)."""
    pending = meta.get("pending")
    if not pending:
        raise cad.CadError("There is no plan waiting for approval")
    if plan_id and plan_id != pending["id"]:
        raise cad.CadError("That plan is no longer the pending one")
    if meta["head"] != pending["base_version"]:
        meta.pop("pending", None)
        cad.save(meta)
        raise cad.CadError("The model changed since this plan was previewed; ask again")
    records = []
    for i, s in enumerate(pending["steps"], 1):
        result, rec = await tools.execute(meta, s["tool"], dict(s["args"]))
        if rec is not None:
            records.append(rec)
        if not result.get("ok"):
            await cad.discard_uncommitted(meta)
            meta.pop("pending", None)
            cad.save(meta)
            return (f"Step {i} of {len(pending['steps'])} ({s['label']}) failed: {result.get('error')}\n"
                    f"The whole plan was rolled back; the model is still version {meta['head']}."), records, False
    meta.pop("pending", None)
    v = await cad.commit_version(meta, f"Plan: {pending['summary']}"[:300])
    lines = [f"Applied the plan ({len(pending['steps'])} step(s)) as version {v}:"]
    for r in records:
        lines.append(f"- {r['message'].splitlines()[0]}")
    return "\n".join(lines), records, True


def cancel(meta):
    had = meta.pop("pending", None)
    cad.save(meta)
    return had is not None


def describe(pending):
    """The chat text for a previewed plan."""
    if not pending["ok"]:
        return f"I tried a preview of: {pending['summary']}\nIt can't be done as asked: {pending['error']}\nNothing was changed."
    lines = [f"**Preview** — {pending['summary']}", ""]
    for i, (s, r) in enumerate(zip(pending["steps"], pending["records"]), 1):
        shown = ", ".join(v.get("display", "") for v in (r or {}).get("verified") or [] if v.get("display"))
        lines.append(f"{i}. `{s['label']}`" + (f" — {shown}" if shown else ""))
        for w in (r or {}).get("warnings") or []:
            lines.append(f"   ⚠ {w}")
    lines += ["", "The 3D view shows the result as a ghost. **Apply** to make it a new version, or **Cancel**. "
                  "(You can also type *apply* or *cancel*.)"]
    return "\n".join(lines)
