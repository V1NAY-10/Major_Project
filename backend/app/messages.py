"""Chat message persistence (MongoDB, or in-memory when no database is configured)."""

import datetime
import itertools
import os

from .db import get_db

_memory: dict[str, list] = {}
_mem_ids = itertools.count(1)


def _jsonable(doc):
    doc = dict(doc)
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    if isinstance(doc.get("created_at"), datetime.datetime):
        doc["created_at"] = doc["created_at"].isoformat()
    return doc


async def add(session_id, role, content, operations=None, version=None, kind=None, selection=None, plan=None,
              choices=None, options=None):
    doc = {
        "session_id": session_id,
        "role": role,
        "content": content,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    }
    if operations:
        doc["operations"] = operations
    if version is not None:
        doc["version"] = version
    if kind:
        doc["kind"] = kind
    if selection:
        doc["selection"] = selection
    if plan:
        doc["plan"] = plan  # a previewed plan waiting for approval (Phase 3)
    if choices:
        doc["choices"] = choices  # clickable answers to a question
    if options:
        doc["options"] = options  # alternatives offered for an open-ended request, each previewed
    db = get_db()
    if db is not None:
        res = await db.messages.insert_one(doc)
        doc["_id"] = res.inserted_id
    else:
        doc["_id"] = f"m{next(_mem_ids)}"
        _memory.setdefault(session_id, []).append(doc)
    return _jsonable(doc)


async def list_messages(session_id, limit=1000):
    db = get_db()
    if db is not None:
        cursor = db.messages.find({"session_id": session_id}).sort("created_at", 1)
        return [_jsonable(d) for d in await cursor.to_list(length=limit)]
    return [_jsonable(d) for d in _memory.get(session_id, [])[-limit:]]


HISTORY_MESSAGES = int(os.getenv("LLM_HISTORY_MESSAGES", "8"))
HISTORY_MESSAGE_CHARS = int(os.getenv("LLM_HISTORY_MESSAGE_CHARS", "1500"))


async def history_for_llm(session_id, limit=HISTORY_MESSAGES):
    """Recent turns as plain {role, content}; assistant turns carry a note of the edits they made."""
    out = []
    for m in (await list_messages(session_id))[-limit:]:
        if m.get("kind") == "error" or m["role"] not in ("user", "assistant"):
            continue
        content = m.get("content") or ""
        if len(content) > HISTORY_MESSAGE_CHARS:
            content = content[:HISTORY_MESSAGE_CHARS] + " …[truncated]"
        if m.get("selection"):
            content += f"\n[Selected: {', '.join(m['selection'])}]"
        ops = [o for o in m.get("operations") or [] if o.get("ok")]
        if ops:
            content += "\n[Edits applied: " + "; ".join(o["message"].split("\n")[0] for o in ops) + "]"
        if m.get("plan"):
            content += f"\n[Plan proposed, waiting for the user's approval: {m['plan'].get('summary', '')}]"
        if m.get("options"):
            content += "\n[Options offered (the user can pick one by number): " + "; ".join(
                f"{o['key']}. {o['title']}" + ("" if o.get("ok") else " (not possible)")
                for o in m["options"].get("options") or []) + "]"
        if m.get("choices"):
            content += "\n[Choices offered: " + ", ".join(f"{c['id']} {c['label']}" for c in m["choices"]) + "]"
        if m.get("version") is not None and m["role"] == "assistant":
            content += f"\n[Model version {m['version']}]"
        out.append({"role": m["role"], "content": content})
    return out
