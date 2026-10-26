"""
Pending plans: previews (dry runs) of one or more edits, applied as one version only when the user approves.

  preview(meta, steps)  run the steps without keeping them: one step uses the engine's dry run (transaction aborted);
                        several steps are applied for real, the result is meshed, then the head version is restored —
                        either way the model ends up exactly as it was. The plan waits in meta["pending"].
  apply(meta)           run the pending steps for real; if step N fails, everything is rolled back to the version the
                        plan started from; otherwise the steps become one new version.
  cancel(meta)          drop the pending plan.
  propose_options(...)  dry-run 2-4 alternative ways to do an open-ended request (meta["options"]);
  apply_option(meta, k) apply the alternative the user picked, like a plan.
"""

import copy
import shutil
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


async def _dry_run(meta, steps):
    """Run the steps without keeping them; the result mesh is written to preview.stl. Returns (records, ok, error)."""
    sid = meta["session_id"]
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
    return records, ok, error


def _clean_steps(steps):
    steps = [{"tool": s["tool"], "args": dict(s.get("args") or {})} for s in steps]
    bad = [s["tool"] for s in steps if s["tool"] not in tools.EDIT_TOOLS]
    if not steps or bad:
        raise cad.CadError("A plan lists edit tool calls only" + (f" (not {', '.join(bad)})" if bad else ""))
    return steps


async def preview(meta, steps, summary="", source="agent"):
    """Dry-run the steps and store them as meta["pending"]. Returns the pending plan."""
    steps = _clean_steps(steps)
    sid = meta["session_id"]
    head = meta["head"]
    records, ok, error = await _dry_run(meta, steps)
    pending = {"id": uuid.uuid4().hex[:10], "summary": summary or "; ".join(label(s) for s in steps),
               "steps": [{**s, "label": label(s)} for s in steps], "records": records, "ok": ok, "error": error,
               "base_version": head, "source": source, "created_at": cad._now(),
               "mesh_url": f"/cad/preview-mesh/{sid}?v={head}&p=" if ok else None}
    if ok:
        pending["mesh_url"] += pending["id"]
    meta["pending"] = pending
    cad.save(meta)
    return pending


# ── Alternatives: several ways to do an open-ended request, each previewed; the user picks one ──────────────────

MAX_OPTIONS = 4


async def propose_options(meta, question, options):
    """Dry-run each alternative (a list of edit steps) and keep them as meta["options"] until the user picks one.
    An alternative that fails its dry run stays in the list, marked not possible, with the reason."""
    sid = meta["session_id"]
    head = meta["head"]
    set_id = uuid.uuid4().hex[:10]
    out = []
    for n, opt in enumerate(options[:MAX_OPTIONS], 1):
        title = str(opt.get("title") or f"Option {n}")[:80]
        try:
            steps = _clean_steps(opt.get("steps") or [])
        except cad.CadError as e:
            out.append({"key": n, "title": title, "description": str(opt.get("description") or "")[:300],
                        "steps": [], "records": [], "ok": False, "error": str(e), "mesh_url": None})
            continue
        records, ok, error = await _dry_run(meta, steps)
        mesh_url = None
        if ok:
            shutil.copyfile(cad.preview_mesh_path(sid), option_mesh_path(sid, n))
            mesh_url = f"/cad/preview-mesh/{sid}?opt={n}&v={head}&p={set_id}"
        out.append({"key": n, "title": title, "description": str(opt.get("description") or "")[:300],
                    "steps": [{**s, "label": label(s)} for s in steps], "records": records, "ok": ok,
                    "error": error, "mesh_url": mesh_url})
    meta.pop("pending", None)
    meta["options"] = {"id": set_id, "question": str(question or "")[:300], "options": out, "base_version": head,
                       "created_at": cad._now()}
    cad.save(meta)
    return meta["options"]


def option_mesh_path(session_id, key):
    return cad.preview_mesh_path(session_id).with_name(f"preview_opt{int(key)}.stl")


async def apply_option(meta, key, set_id=None):
    """Apply one previewed alternative as one version. Returns (reply text, records, ok)."""
    offer = meta.get("options")
    if not offer or (set_id and set_id != offer["id"]):
        raise cad.CadError("Those options are no longer current; ask again")
    if meta["head"] != offer["base_version"]:
        meta.pop("options", None)
        cad.save(meta)
        raise cad.CadError("The model changed since these options were previewed; ask again")
    opt = next((o for o in offer["options"] if o["key"] == int(key)), None)
    if opt is None:
        raise cad.CadError(f"There is no option {key}")
    if not opt["ok"]:
        raise cad.CadError(f"Option {key} ({opt['title']}) is not possible: {opt['error']}")
    meta.pop("options", None)
    meta["pending"] = {"id": offer["id"], "summary": f"Option {key}: {opt['title']}", "steps": opt["steps"],
                       "base_version": offer["base_version"], "ok": True}
    return await apply(meta)


def describe_options(offer):
    """The chat text for a set of alternatives."""
    good = [o for o in offer["options"] if o["ok"]]
    lines = [offer["question"] or "There are several ways to do this. Pick one:", ""]
    for o in offer["options"]:
        if not o["ok"]:
            continue
        shown = ", ".join(v.get("display", "") for r in o["records"] for v in (r or {}).get("verified") or []
                          if v.get("display"))
        lines.append(f"**{o['key']}. {o['title']}** — {o['description']}" + (f" ({shown})" if shown else ""))
    bad = [o for o in offer["options"] if not o["ok"]]
    if bad:
        lines += ["", "Also considered, but not possible on this model:"]
        lines += [f"- {o['title']}: {o['error']}" for o in bad]
    if good:
        lines += ["", "Hover or click **Preview** to see an option as a ghost in the 3D view, then **Apply** it "
                      "(or type *option 2*). Nothing has been changed yet."]
    else:
        lines += ["", "None of the approaches works on this model, so nothing was changed."]
    return "\n".join(lines)


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
    had = meta.pop("pending", None) or meta.pop("options", None)
    meta.pop("options", None)
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
